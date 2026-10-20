# filepath: src/rt_push/common.py
"""共享基础工具：时区/类型防御/字符串相似度/时间解析（自 scripts/real_time_push.py 拆出，行为不变）"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone


logger = logging.getLogger("real_time_push")



BJT = timezone(timedelta(hours=8))


def _state_timestamp(value: str):
    """把状态中持久化的北京时间字符串转换为 epoch 秒。"""
    try:
        parsed = datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=BJT).timestamp()


def _as_list(v):
    """LLM 字段类型防御：字符串→单元素列表，杜绝 set()/join 拆字

    2026-08-13 P1 修复：LLM 返回 sectors/entities/affected_stocks 为字符串
    （非 JSON 数组）时，`set('半导体')` 会拆成 {'半','导','体'}、`' '.join('半导体')`
    变 '半 导 体'——科技词匹配失效（漏推科技）、同事件合并/饱和计数失真。
    统一收敛为：None→[]；str→[v]；list/tuple/set→list；其他→[]。
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    return []


def _env_int(name: str, default: int) -> int:
    """安全读取整数环境变量：非法值回退默认并告警（2026-08-13 P2 修复）

    此前 RT_TOPIC_LIMIT 模块级 int() 无防御，env 非法值 → import 即崩（云端 CI 同挂）。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning(f"环境变量 {name} 无效: {raw!r}，使用默认值 {default}")
        return default


def _parse_bjt(value: str):
    """状态时间字符串（北京时间）转 aware datetime，失败返回 None"""
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    except (TypeError, ValueError):
        return None


# ============================================================
# 推送级事件去重（跨源同事件只推一次）
# ============================================================
def _to_float(v, default: float = 0.0) -> float:
    """宽容转 float（LLM 返回的 score 可能是字符串/None）"""
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return default


def _as_bool(v, default: bool = False) -> bool:
    """严格布尔解析（LLM 返回的 push/is_leader_stock 可能是字符串）

    2026-08-07 修复：此前用 bool(e.get("push"))——LLM 输出 "push": "false"
    （带引号字符串，非标准 JSON）时 bool("false")==True → 误判为要推。
    本函数: bool 直通；数字按真值；字符串按 true/1/yes 等白名单解析，
    无法识别时返回 default（宁可保守不推，也不因类型误判漏推/滥推）。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v or "").strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    return default


# 实体别名归一化表（2026-08-13 修复：LLM 对同一实体的表述不稳定——"大摩" vs "摩根士丹利"、
# "三星电子" vs "三星"、英文 vs 中文，导致跨源同事件合并失效、重复推送）。
# 仅收"无歧义"的别名（简称→规范全称/中英对照），不收录有歧义的简称。
_ENTITY_ALIAS = {
    "大摩": "摩根士丹利", "小摩": "摩根大通",
    "TSMC": "台积电", "三星电子": "三星", "Samsung": "三星",
    "海力士": "SK海力士", "SK Hynix": "SK海力士",
    "NVIDIA": "英伟达", "Tesla": "特斯拉", "Apple": "苹果",
    "Google": "谷歌", "Alphabet": "谷歌", "Microsoft": "微软",
    "Amazon": "亚马逊", "欧洲央行": "欧央行", "ECB": "欧央行",
    "日央行": "日本央行", "BOJ": "日本央行", "Fed": "美联储",
    # 2026-09-22 Q-01：长鑫科技/长鑫存储/CXMT 是同一公司，跨源 LLM 实体抽取会漂移。
    "长鑫存储": "长鑫科技", "CXMT": "长鑫科技",
    "上海市": "上海", "北京市": "北京",
}


def _normalize_entity(e: str) -> str:
    """实体归一化：剥离股票代码后缀（台积电(TSM.N)→台积电）+ 别名映射"""
    if not e:
        return e
    e = str(e).strip()
    # 剥离末尾股票代码后缀 "(TSM.N)" / "(AAPL.O)" / "(00700.HK)"
    e = re.sub(r"[（(][A-Za-z0-9.\-]+[)）]$", "", e).strip()
    return _ENTITY_ALIAS.get(e, e)


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（连续，归一化标题≤40字，DP O(n·m) 开销可忽略）"""
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


def _lcs_subseq_len(a: str, b: str) -> int:
    """最长公共子序列长度（允许跨越插入，DP O(n·m)）

    与连续子串 _lcs_len 的区别: 允许中间插入/替换字符仍算匹配，
    可捕获同事件报道中"存储ETF" vs "内存ETF"这类单字差异
    （连续子串 LCS 会因单字不同被拆断，_is_same_event 兜底失效实证）。
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0
    prev = [0] * (m + 1)
    for i in range(1, n + 1):
        cur = [0] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = cur[j - 1] if cur[j - 1] >= prev[j] else prev[j]
        prev = cur
    return prev[m]
