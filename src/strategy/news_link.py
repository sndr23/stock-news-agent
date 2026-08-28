# -*- coding: utf-8 -*-
"""
资讯<->策略 三层协同桥（news_link.py）
====================================================
把资讯流（real_time_push）与因子流（factor_collector）的产品接进合理配置层，
实现"事件驱动 + 组合优化"的浅耦合协同，全部可独立开关、失败降级。

L1 报告织入：读取当日已推事件，匹配持仓/候选股相关资讯，织入策略日报。
L2 事件->alpha 修正：检索个股级强方向事件，对当日 alpha 温度修正（低利空、
   利多事件叠加），并把策略持仓股回写 watchlist.json，使资讯流对持仓股优先。
L3 宏观 overlay：聚合 factor_state.json 的快照指数基差、两市资金流、
   risk_state 新建热门仓位系数，修正 _risk_overlay 的 exposure。

只读外部状态（Gist / 本地 logs/*.json），从不写 Gist，避免与
real_time_push/factor_collector 的单写端冲突。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from src.strategy.state_io import get_gist_config

from .data_freshness import BJT, is_recent_data_date

logger = logging.getLogger("strategy.news_link")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 与 factor_collector 保持一致的状态文件名与路径（只读）
REALTIME_STATE_FILENAME = "real_time_state.json"
FACTOR_STATE_FILENAME = "factor_state.json"
_REALTIME_STATE_PATH = PROJECT_ROOT / "logs" / "real_time_state.json"
_FACTOR_STATE_PATH = PROJECT_ROOT / "logs" / "factor_state.json"
WATCHLIST_PATH = PROJECT_ROOT / "watchlist.json"

# 强方向事件（L2 温度修正与 L1 显著标注只用这些）
STRONG_DIRECTIONS = {"bullish", "bearish"}
# direction -> (中文标注, 事件对 alpha 的方向修正符号)
_DIR_LABEL = {
    "bullish": ("利多", +1.0),
    "mildly_bullish": ("偏多", +0.5),
    "neutral": ("中性", 0.0),
    "mixed": ("分歧", 0.0),
    "mildly_bearish": ("偏空", -0.5),
    "bearish": ("利空", -1.0),
}

# 宏观 overlay（L3）：annual_pct 以百分点存储（-12.11 = -12.11%，对齐 factor_collector）
_IC_DEEP_SHORT_PCT = -4.0
_FUND_FLOW_LOW_YI = -80.0  # 主力单日净流出 > 80 亿（单位：亿）
_CITIC_NET_SHORT_LOTS = -2000  # 中信全合约当日净加空 > 2000 手 → 降仓


def _http_get_json(url: str, timeout: int = 15) -> dict:
    req = Request(url, headers={"Accept": "application/vnd.github+json",
                                "User-Agent": "strategy-news-link"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_realtime_state() -> dict:
    """读资讯流状态（配置 Gist 时云端唯一来源，否则读本地）。只读。"""
    gist_token, gist_id = get_gist_config()
    if gist_token and gist_id:
        # Gist 是跨容器资讯状态的权威来源；失败时禁止使用本地旧事件，
        # 否则资讯联动可能把旧日事件误算进当日策略上下文。
        return _read_gist_file(REALTIME_STATE_FILENAME, strict=True)
    try:
        state = json.loads(_REALTIME_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def load_factor_state() -> dict:
    """读因子状态（配置 Gist 时云端唯一来源，否则读本地）。只读。"""
    gist_token, gist_id = get_gist_config()
    if gist_token and gist_id:
        # Gist 是跨容器状态的权威来源；请求失败或内容损坏时不能拿本地旧快照
        # 继续运行，否则旧因子可能悄悄改变当日仓位。
        return _read_gist_file(FACTOR_STATE_FILENAME, strict=True)
    return _read_local(_FACTOR_STATE_PATH)


CITIC_POS_STATE_FILENAME = "citic_pos_state.json"
_CITIC_POS_STATE_PATH = PROJECT_ROOT / "logs" / "citic_pos_state.json"


def load_citic_pos_state() -> dict:
    """读中信期货净持仓（配置 Gist 时云端唯一来源，否则读本地）。只读。"""
    gist_token, gist_id = get_gist_config()
    if gist_token and gist_id:
        # 同一 Gist 配置下禁止云端失败后混用本地旧持仓。
        return _read_gist_file(CITIC_POS_STATE_FILENAME, strict=True)
    return _read_local(_CITIC_POS_STATE_PATH)


def _read_gist_file(filename: str, strict: bool = False) -> dict:
    """从共用 Gist 读指定状态文件。

    默认模式供只读增强数据使用，失败/无配置返回空；strict 模式供有写回
    语义的状态使用，读取异常必须抛出，且成功但缺文件仍明确返回空状态。
    """
    gist_token, gist_id = get_gist_config()
    if not (gist_token and gist_id):
        return {}
    try:
        url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
        req = Request(url, headers={"Authorization": f"token {gist_token}",
                                    "Accept": "application/vnd.github+json",
                                    "User-Agent": "strategy-news-link"})
        with urlopen(req, timeout=15) as resp:
            g = json.loads(resp.read().decode("utf-8"))
        fobj = (g.get("files") or {}).get(filename)
        if fobj is not None:
            content = fobj.get("content")
            if strict and (not isinstance(content, str) or not content.strip()):
                raise ValueError(f"Gist 状态文件 {filename} 内容为空")
            state = json.loads(content or "{}")
            if strict and not isinstance(state, dict):
                raise ValueError(f"Gist 状态文件 {filename} 根节点不是对象")
            return state
    except Exception as e:
        logger.warning("Gist 状态文件 %s 读取失败: %s", filename, type(e).__name__)
        if strict:
            raise RuntimeError(f"Gist 状态文件 {filename} 读取失败，拒绝回退本地") from e
    return {}


def _read_local(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def recent_pushed_events(state: dict, hours: float = 48.0) -> list:
    """从资讯流 state 提取近 N 小时已推事件列表（倒序，含 title_norm/dir/stocks/sectors）"""
    now = datetime.now(BJT)
    out = []
    for e in (state.get("pushed_events") or []):
        t = str(e.get("t") or "")
        try:
            ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
        except ValueError:
            continue
        if not (0 <= (now - ts).total_seconds() <= hours * 3600):
            continue
        out.append(e)
    out.sort(key=lambda x: x.get("t", ""), reverse=True)
    return out


def _event_key(e: dict) -> str:
    """事件去重键（日期+事件签名），供 news 维度并集去重。"""
    return (str(e.get("t") or "")[:10] + "#"
            + "|".join(sorted(e.get("entities") or [])) + "#"
            + "|".join(sorted(e.get("events") or [])) + "#"
            + "|".join(sorted(e.get("numbers") or [])) + "#"
            + str(e.get("title_norm") or ""))


def today_news_events(state: dict, today: str = None) -> list:
    """今日资讯输入 = 已推送 ∪ 当日预筛候选（P7-1 2026-08-22）。

    此前 news_modifier 仅能读"当日强档已推送"；real_time_push 现把每个经 LLM 判定
    的重大候选（含方向）持久化为 candidate_events，使资讯维度覆盖全部重大候选。
    按 (日期, 事件签名) 去重（已推送的候选同时在 pushed_events 与 candidate_events，
    并集去重避免重复计权），倒序返回。方向由 dir 字段（LLM 判定）承载。
    """
    today = today or datetime.now(BJT).strftime("%Y-%m-%d")
    ded = {}
    for e in (*((state.get("pushed_events")) or []),
              *((state.get("candidate_events")) or [])):
        _t = str(e.get("t") or "")
        if not _t.startswith(today):
            continue
        ded.setdefault(_event_key(e), e)
    out = list(ded.values())
    out.sort(key=lambda x: x.get("t", "") or "", reverse=True)
    return out


def _norm_name(name: str) -> str:
    """名称归一化：去除空格、常见公司后缀，供匹配使用。"""
    s = str(name or "").strip()
    for suf in ("股份有限公司", "有限责任公司", "公司", "集团", "（港）", "(港)"):
        s = s.replace(suf, "")
    return s.strip()


def _event_text(e: dict) -> str:
    """事件可检索文本：标题 + 板块 + 主体（用于子串匹配，宽容召回）"""
    parts = [str(e.get("title_norm") or ""),
             str(e.get("title") or "")]
    for k in ("sectors", "stocks", "entities"):
        parts.extend(str(x) for x in (e.get(k) or []))
    return " ".join(p for p in parts if p)


def match_events_for_code(events: list, code: str, name: str) -> list:
    """返回与个股 code/name 相关的事件（含匹配证据）。匹配口径：
    code精确命中嵌套实体；name/数名（去后缀）在事件检索文本中作为子串召回。
    返回 [ {…事件, matched:匹配到的词} ]。
    """
    if not events:
        return []
    code_n = str(code).zfill(6)
    probes = {"code": code_n, "code_short": code_n.lstrip("0")}
    name_key = _norm_name(name) if name else ""
    name_short = name_key[:4] if len(name_key) >= 4 else name_key
    out = []
    for e in events:
        matched = None
        list_fields = []
        for k in ("stocks", "entities"):
            list_fields.extend(str(x) for x in (e.get(k) or []))
        for ent in list_fields:
            en = _norm_name(ent)
            if en == name_key or (name_key and en and en in name_key):
                matched = ent
                break
        # 代码精确命中（嵌套实体里含 6 位/去零代码）
        if matched is None:
            for ent in list_fields:
                dig = "".join(ch for ch in ent if ch.isdigit())
                if dig in (code_n, code_n.lstrip("0")):
                    matched = ent
                    break
        # 名称子串召回（宽容层，标题/板块/主体，从长词到短词）
        if matched is None:
            text = _event_text(e)
            if name_key and len(name_key) >= 4 and name_key in text:
                matched = name_key
            elif name_short and len(name_short) >= 3 and name_short in text:
                matched = name_short
        if matched:
            out.append({**e, "matched": matched})
    return out


def related_news_for_holdings(events: list, holdings: dict, names: dict,
                              limit_per_code: int = 3) -> dict:
    """对持仓股聚合相关资讯：{code: [ {…事件, matched} ]}。holdings: {code: weight}，
       names: {code: 名称}。按相关性排序，每股最多 limit 条。
    """
    result = {}
    for code in holdings:
        c = str(code).zfill(6)
        hits = match_events_for_code(events, c, names.get(code) or "")
        if hits:
            result[c] = hits[:limit_per_code]
    return result


def event_alpha_correction(events: list, codes: list, names: dict,
                           alpha_sigma: float = 1.0,
                           strong_only: bool = True) -> dict:
    """L2 事件->alpha 修正：个股级强方向事件对股票方向的修正。
    返回 {code: correction}。sign 已含强度编码（strong=±1.0 / mild=±0.5）。
    默认仅强方向事件产生非零修正；strong_only=False 时 mild 也用半强度。
    """
    corr: dict[str, float] = {}
    for code in codes:
        c = str(code).zfill(6)
        hits = match_events_for_code(events, c, names.get(code) or "")
        if not hits:
            continue
        strength = 0.0
        for e in hits[:2]:  # 取最相关 2 条，取最强方向（防重复叠加）
            d = str(e.get("dir") or "")
            _, sign = _DIR_LABEL.get(d, ("", 0.0))
            if strong_only and d not in STRONG_DIRECTIONS:
                continue
            if abs(sign) > abs(strength):
                strength = sign
        if strength != 0.0:
            corr[c] = strength * alpha_sigma
    return corr


def apply_alpha_correction(alpha: dict, corr: dict,
                           scale: float = 1.0) -> dict:
    """把事件修正叠加到当日 alpha 截面（win 与 loss 方向一致），返回新 alpha dict。"""
    if not corr:
        return alpha
    out = dict(alpha)
    for c, v in corr.items():
        if c in out:
            out[c] = float(out[c]) + v * scale
    return out


def watchlist_holdings_state(holdings: dict, names: dict) -> dict:
    """L2 反向：把策略持仓股转为 watchlist.json 的 stocks 条目（供资讯流优先）。
    返回可直接并入 watchlist 的 stock dict 列表（不写盘，由调用方合并）。
    """
    entries = []
    for code in holdings:
        c = str(code).zfill(6)
        n = names.get(code) or str(code)
        entries.append({"name": n, "code": c, "source": "strategy"})
    return entries


def merge_watchlist_holdings(watchlist: dict, holdings: dict, names: dict) -> dict:
    """把策略持仓并回 watchlist.json 结构（浅耦合：保留用户自选，追加策略条）"""
    merged = dict(watchlist)
    existing = merged.get("stocks", []) or []
    # 记录现存 code/name，避免重复
    have_codes = set()
    have_names = set()
    new_add = []
    for s in existing:
        if isinstance(s, dict):
            if s.get("code"):
                have_codes.add(str(s["code"]).zfill(6))
            if s.get("name"):
                have_names.add(_norm_name(str(s["name"])))
    for code in holdings:
        c = str(code).zfill(6)
        n = names.get(code) or ""
        if c in have_codes or (_norm_name(n) and _norm_name(n) in have_names):
            continue
        new_add.append({"name": n or c, "code": c, "source": "strategy"})
    merged["stocks"] = existing + new_add
    return merged


def macro_exposure(state_factor: dict,
                   base_exposure: float = 1.0,
                   citic_state: dict = None,
                   today: date = None) -> dict:
    """L3 宏观 overlay：读 factor_state 快照 + 中信期货净持仓，产出仓位系数修正。
    返回 {"factor": 修正系数, "reasons": [原因], "exposure": base*factor}。
    约束：只读、向下修正（顶部风险），IC 深度贴水/资金大幅流出/中信大幅加空时减仓。
    """
    snapshot = (state_factor.get("snapshot") or {}) or {}
    reasons = []
    factor = 1.0
    basis = snapshot.get("basis") or {}
    for code in ("IF", "IC", "IM", "IH"):
        b = (basis.get(code) or {})
        ap = _to_float(b.get("annual_pct"))
        if ap is not None and ap <= _IC_DEEP_SHORT_PCT:
            reasons.append(f"{code}年化贴水 {ap:.1f}%（深度贴水，谨慎）")
            factor = min(factor, 0.95)
    flows = snapshot.get("flows") or {}
    main_net = _to_float(flows.get("main_net_yi"))
    if main_net is not None and main_net <= _FUND_FLOW_LOW_YI:
        reasons.append(f"两市主力净流出 {main_net:.0f} 亿（避险）")
        factor = min(factor, 0.92)
    risk_state = str(snapshot.get("risk_state") or "")
    _RISK_OFF = "risk_off"
    if risk_state == _RISK_OFF:
        reasons.append("风险状态：risk_off（风险收缩期）")
        factor = min(factor, 0.92)
    elif risk_state:
        reasons.append(f"风险状态：{risk_state}")

    # 中信日报通常在收盘后才公布，允许使用上一交易日；更早的记录不能继续
    # 影响基金轮动仓位。按日期挑选最近的新鲜记录，兼容历史列表无序情况。
    citic_hist = (citic_state or {}).get("pos_history") or []
    citic_latest = None
    citic_stale = bool(citic_hist)
    today = today or datetime.now(BJT).date()
    if isinstance(citic_hist, list):
        fresh = []
        for item in citic_hist:
            if not isinstance(item, dict):
                continue
            day = str(item.get("day") or "")[:10]
            if is_recent_data_date(day, max_lag_days=1, calendar="cn", today=today):
                fresh.append((day, item))
        if fresh:
            citic_latest = max(fresh, key=lambda pair: pair[0])[1]
            citic_stale = False
    if citic_stale and citic_latest is None:
        reasons.append("中信持仓数据过期或格式无效，忽略该增强项")
    if citic_latest:
        net = _to_float((citic_latest.get("net") or {}).get("_total"))
        if net is not None:
            day = str(citic_latest.get("day") or "")
            if net <= _CITIC_NET_SHORT_LOTS:
                reasons.append(f"中信全合约净加空 {int(net)} 手（{day}，谨慎）")
                factor = min(factor, 0.9)
            else:
                reasons.append(f"中信净持仓 {int(net)} 手（{day}）")
    if not reasons:
        reasons.append("宏观状态正常，维持基准仓位")
    return {"factor": round(factor, 3),
            "exposure": round(base_exposure * factor, 4),
            "reasons": reasons}


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def format_event_line(e: dict) -> str:
    """资讯事件 → 一行 markdown（L1 报告织入用）。"""
    title = str(e.get("title_norm") or e.get("title") or "(无标题)")
    d, _ = _DIR_LABEL.get(str(e.get("dir") or ""), ("未知", 0.0))
    t = str(e.get("t") or "")[-5:]
    return f"- [{d}] {title}（{t}）"
