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
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

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

# 宏观 overlay（L3）：当 IC 深度贴水（年化≤-4%）扩大杠杆权重
_IC_DEEP_SHORT_BPS = -0.04
_FUND_FLOW_LOW_YI = -80.0  # 主力单日净流出 > 80 亿
_CITIC_NET_SHORT_LOTS = -2000  # 中信全合约当日净加空 > 2000 手 → 降仓


def _http_get_json(url: str, timeout: int = 15) -> dict:
    req = Request(url, headers={"Accept": "application/vnd.github+json",
                                "User-Agent": "strategy-news-link"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_realtime_state() -> dict:
    """读资讯流状态（云端 Gist 优先，失败/无配置降级本地）。只读，永不写回。"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            req = Request(url, headers={"Authorization": f"token {gist_token}",
                                        "Accept": "application/vnd.github+json",
                                        "User-Agent": "strategy-news-link"})
            with urlopen(req, timeout=15) as resp:
                g = json.loads(resp.read().decode("utf-8"))
            fobj = (g.get("files") or {}).get(REALTIME_STATE_FILENAME)
            if fobj is not None:
                return json.loads(fobj.get("content") or "{}")
        except Exception as e:
            logger.warning("Gist 资讯状态读取失败，降级本地: %s", type(e).__name__)
    try:
        return json.loads(_REALTIME_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_factor_state() -> dict:
    """读因子状态（云端 Gist 优先，本地降级）。只读。"""
    return _read_gist_file(FACTOR_STATE_FILENAME) or _read_local(_FACTOR_STATE_PATH)


CITIC_POS_STATE_FILENAME = "citic_pos_state.json"
_CITIC_POS_STATE_PATH = PROJECT_ROOT / "logs" / "citic_pos_state.json"


def load_citic_pos_state() -> dict:
    """读中信期货净持仓状态（云端 Gist 优先，本地降级）。只读。"""
    return _read_gist_file(CITIC_POS_STATE_FILENAME) or _read_local(_CITIC_POS_STATE_PATH)


def _read_gist_file(filename: str) -> dict:
    """从共用 Gist（GIST_ID）读指定状态文件；失败/无配置返回 {}"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
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
            return json.loads(fobj.get("content") or "{}")
    except Exception as e:
        logger.warning("Gist 状态文件 %s 读取失败: %s", filename, type(e).__name__)
    return {}


def _read_local(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def recent_pushed_events(state: dict, hours: float = 48.0) -> list:
    """从资讯流 state 提取近 N 小时已推事件列表（倒序，含 title_norm/dir/stocks/sectors）"""
    now = datetime.now()
    out = []
    for e in (state.get("pushed_events") or []):
        t = str(e.get("t") or "")
        try:
            ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if not (0 <= (now - ts).total_seconds() <= hours * 3600):
            continue
        out.append(e)
    out.sort(key=lambda x: x.get("t", ""), reverse=True)
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
                   citic_state: dict = None) -> dict:
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
        if ap is not None and ap <= _IC_DEEP_SHORT_BPS:
            reasons.append(f"{code}年化贴水 {ap*100:.1f}%（深度贴水，谨慎）")
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

    # 中信期货全合约净持仓方向（L3 目标要求 2026-08-21）：取 pos_history 最新一条
    citic_hist = (citic_state or {}).get("pos_history") or []
    citic_latest = citic_hist[-1] if citic_hist else None
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