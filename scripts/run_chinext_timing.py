# -*- coding: utf-8 -*-
"""
创业板仓位信号入口（run_chinext_timing.py）
====================================================
每个交易日 14:30（北京）云端触发，推送创业板方向（CPO/PCB 场外基金）
的目标仓位建议，用户 15:00 截单前手动执行。

数据链（全部带降级，缺一维缩一维，永不无信号）：
  399006 日线(东财/腾讯) + 盘中实时(腾讯)
  + factor_state 快照（贴水/资金流/涨停情绪/宽度/波动分位/risk_state）
  + citic_pos_state（中信全合约净持仓）
  + 当日已推资讯事件（LLM 方向 × 科技相关度）

用法：
  python scripts/run_chinext_timing.py --dry-run    # 本地打印
  python scripts/run_chinext_timing.py --push       # 推送 + 写状态（云端）
  python scripts/run_chinext_timing.py --backtest   # 核心层历史回测验证
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chinext_timing")

from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import chinext_factors as cf  # noqa: E402
from src.strategy import chan_light as ch  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402
from src.strategy import news_link as nl  # noqa: E402
from src.strategy import overseas as ovs  # noqa: E402
from src.strategy.data import (load_index_daily_full, load_index_sina,
                               load_stock_sina)  # noqa: E402
from src.strategy.fund_data import get_quotes  # noqa: E402

TIMING_STATE_FILENAME = "chinext_timing_state.json"
_LOCAL_STATE_PATH = PROJECT_ROOT / "logs" / TIMING_STATE_FILENAME
SYMBOL = "399006"
SYMBOL_STOCK = "300308"  # 中际旭创（科技龙头情绪标的，双确认个股侧）
HISTORY_LIMIT = 120  # 影子 IC 记录条数上限


# ---------------- 状态持久化（Gist 单写端：本 workflow） ----------------

def load_state() -> dict:
    """云端 Gist 优先，本地降级。"""
    state = nl._read_gist_file(TIMING_STATE_FILENAME)
    if state:
        return state
    try:
        return json.loads(_LOCAL_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> bool:
    """写本地 + 尽力写 Gist（云端跨容器持久化）。"""
    try:
        _LOCAL_STATE_PATH.parent.mkdir(exist_ok=True)
        _LOCAL_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning("本地状态写入失败: %s", e)
    token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if not (token and gist_id):
        return True  # 本地已写，无 Gist 配置（本地模式）
    import requests
    url = f"https://api.github.com/gists/{gist_id}"
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "stock-news-agent-chinext-timing"}
    payload = {"files": {TIMING_STATE_FILENAME: {
        "content": json.dumps(state, ensure_ascii=False, indent=1)}}}
    import time
    for attempt in range(3):
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            return True
        except Exception as e:
            if attempt == 2:
                logger.warning("Gist 状态写入失败（本地已保底）: %s", e)
            else:
                time.sleep(2 ** (attempt + 1))
    return False


# ---------------- 数据聚合 ----------------

def gather_context(df) -> dict:
    """聚合全部数据源，任何一源失败独立降级。"""
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    # 缠论需 high/low（新浪全量链提供；备用链缺则退化跳过缠论）
    highs = (df["high"].tolist() if "high" in df else [])
    lows = (df["low"].tolist() if "low" in df else [])

    intraday = 0.0
    try:
        q = get_quotes([f"0.{SYMBOL}"])
        intraday = float(q.get(SYMBOL) or 0.0)
    except Exception as e:
        logger.warning("盘中行情失败（降级为0）: %s", type(e).__name__)

    snapshot = {}
    citic_net = None
    citic_day = ""
    events = []
    try:
        fs = nl.load_factor_state()
        snapshot = fs.get("snapshot") or {}
    except Exception as e:
        logger.warning("factor_state 读取失败: %s", type(e).__name__)
    try:
        cs = nl.load_citic_pos_state()
        hist = cs.get("pos_history") or []
        if hist:
            citic_net = float((hist[-1].get("net") or {}).get("_total") or 0.0)
            citic_day = str(hist[-1].get("day") or "")
    except Exception as e:
        logger.warning("citic 状态读取失败: %s", type(e).__name__)
    try:
        rt = nl.load_realtime_state()
        today = datetime.now().strftime("%Y-%m-%d")
        events = [e for e in nl.recent_pushed_events(rt, hours=30)
                  if str(e.get("t") or "").startswith(today)]
    except Exception as e:
        logger.warning("资讯事件读取失败: %s", type(e).__name__)

    # 外盘 t-1 隔夜跌幅（硬风控盘中急跌的同源确认，缺源降级为 0 不阻断）
    overseas_drop = 0.0
    try:
        ov = ovs.load_overseas(PROJECT_ROOT)
        overseas_drop = ovs.overnight_drop(ov, datetime.now())
    except Exception as e:
        logger.warning("外盘状态读取失败（降级为0）: %s", type(e).__name__)

    # 中际旭创（双确认个股侧）：趋势/动量/盘中，缺源降级为 None（跳过确认）。
    # 用 ≤d-1 完整日收盘算趋势/动量（当日 partial close 会污染均线，与量价修复同口径），
    # 当日实时涨跌幅单独注入 stock_ctx，14:45 旭创当日走势才真正进入双确认。
    stock_ctx = None
    try:
        sdf = load_stock_sina(SYMBOL_STOCK)
        if sdf is not None and not sdf.empty:
            scloses_full = sdf["close"].tolist()
            sdates = [d.strftime("%Y-%m-%d") for d in sdf.index]
            today_s = datetime.now().strftime("%Y-%m-%d")
            # 若末根为当日 partial，剔除后再算 trend/mom（与回测一致用完整日）
            scloses = (scloses_full[:-1] if sdates and sdates[-1] == today_s
                       else scloses_full[:])
            st_pct = 0.0
            try:
                st_pct = float((get_quotes([f"0.{SYMBOL_STOCK}"]).get(
                    SYMBOL_STOCK) or 0.0))
            except Exception as e:
                logger.warning("旭创盘中行情失败（当日走势不进双确认）: %s",
                               type(e).__name__)
            stock_ctx = {
                "trend": ct.trend_score(scloses) if len(scloses) >= 30 else None,
                "mom": ct.momentum_score(scloses) if len(scloses) >= 60 else None,
                "last_close": (scloses[-1] if scloses else None),
                "intraday_pct": st_pct,
                "date": str(sdf.index[-1].date()),
            }
    except Exception as e:
        logger.warning("中际旭创数据读取失败（跳过双确认）: %s", type(e).__name__)

    # 修复2：实盘量价口径收口——当日(14:45)新浪返回 partial 累计成交额，
    # 若用它进量能滚动分位会系统性低估量能分位(更容易落"缩量"档→盘中偏空)。
    # 处理：末根若为当日 partial，量能序列用 ≤d-1 完整量占位(保持长度与 close 对齐)，
    # 当日实时量能单独记录 day_amount_ratio 供盘面判断，不进量价因子分位。
    day_amount_ratio = 0.0
    if len(amounts) >= 2 and dates and dates[-1] == datetime.now().strftime("%Y-%m-%d"):
        if amounts[-1] and amounts[-2]:
            day_amount_ratio = amounts[-1] / amounts[-2]
        amounts = list(amounts)
        amounts[-1] = amounts[-2]  # 当日 partial 作废，用昨量占位(长度不变)
    return {"closes": closes, "amounts": amounts, "dates": dates,
            "highs": highs, "lows": lows,
            "intraday": intraday, "snapshot": snapshot,
            "citic_net": citic_net, "citic_day": citic_day, "events": events,
            "overseas_drop": overseas_drop, "stock_ctx": stock_ctx,
            "day_amount_ratio": day_amount_ratio,
            "erp_pctile": _load_erp_basis(dates)}


VAL_SPAN = 500  # 估值分位滚动窗（实盘与回测定稿同口径）


def _load_erp_basis(dates) -> Optional[list]:
    """加载创业板50 TTM PE 滚动分位便宜度序列（对齐 dates，span=VAL_SPAN）。
    缺源/失败返回 None → 核心层估值维自动置 0。dates 为 'YYYY-MM-DD' 字符串列表。"""
    try:
        pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
        if not pe_map:
            return None
        pe = ipe.align_pe_by_dates(pe_map, list(dates))
        pe = [0.0 if v is None else v for v in pe]
        return ipe.pe_to_cheap_pctile(pe, VAL_SPAN)
    except Exception as e:
        logger.warning("估值分位计算失败（降级为0）: %s", type(e).__name__)
        return None


def _dimension_modifier(snapshot: dict, ctx: dict) -> dict:
    """v4 修正层（有界 ±0.30）：派生品贴水 + 资金 + 情绪 + 资讯 + 实时(外盘4评分占位)。
    已按用户要求移除中信净持仓。外盘因子(KOSPI/SOX/VIX/A50)数据源在 Phase C/D 接入，
    接入前该子项恒 0；贴水/资金/情绪/资讯已有 factor_state 源，实时生效。"""
    basis = (snapshot.get("basis") or {}) if snapshot else {}
    ap = []
    for code in ("IC", "IM", "IF"):
        b = basis.get(code) or {}
        try:
            ap.append(float(b.get("annual_pct")))
        except (TypeError, ValueError):
            continue
    worst_ap = min(ap) if ap else None
    # 贴水 ±0.06（不含中信，v4 收敛权重）
    d_score = 0.0
    d_detail = []
    if worst_ap is not None:
        if worst_ap <= -15:
            d_score -= 0.06
        elif worst_ap <= -8:
            d_score -= 0.04
        elif worst_ap <= -4:
            d_score -= 0.02
        elif worst_ap >= 0:
            d_score += 0.03
        d_detail.append(f"IC/IM最差年化{worst_ap:.1f}%")
    # 资金 ±0.05（两市主力净流 + 科技板块方向）
    f_score = ct.flows_modifier(
        (snapshot.get("flows") or {}) if snapshot else {},
        (snapshot.get("sector_flows") or {}) if snapshot else None,
        ct.TECH_KW)["score"]
    # 情绪 ±0.04
    m_score = ct.mood_modifier(
        (snapshot.get("sentiment") or {}) if snapshot else {},
        (snapshot.get("breadth") or {}) if snapshot else {},
        (snapshot.get("option") or {}) if snapshot else None)["score"]
    # 资讯 ±0.06
    dir_sign = {d: sign for d, (_lbl, sign) in nl._DIR_LABEL.items()}
    n_score = ct.news_modifier(ctx["events"], dir_sign)["score"]
    # 外盘已改走硬风控辅助确认通道（见 score_all 的 overseas_drop），不打分
    total = d_score + f_score + m_score + n_score
    return {"score": round(ct.clamp(total, -0.30, 0.30), 3),
            "basis": round(d_score, 3), "flow": round(f_score, 3),
            "mood": round(m_score, 3), "news": round(n_score, 3),
            "detail": "；".join(d_detail)}


def _chan_signal(ctx: dict) -> dict:
    """缠论结构子项（有界 ±0.08，只做结构确认与否决，不进核心回测）。
    顶背驰否决级 -0.06；买卖点/笔方向/中枢位置轻微修正。数据不足或异常降至 0。"""
    highs, lows, closes = ctx.get("highs"), ctx.get("lows"), ctx.get("closes")
    if not highs or not lows or len(lows) != len(closes) or len(lows) < 30:
        return {"score": 0.0, "bustop": False, "bi_dir": "-", "zone": "-",
                "last_signal": "-", "detail": "缠论：数据不足跳过"}
    try:
        cs = ch.chan_state(highs, lows, closes)
    except Exception as e:
        logger.warning("缠论计算失败（降级至0）: %s", type(e).__name__)
        return {"score": 0.0, "bustop": False, "bi_dir": "-", "zone": "-",
                "last_signal": "-", "detail": "缠论：计算失败跳过"}
    s, parts = 0.0, []
    if cs.get("bustop"):
        s -= 0.06; parts.append("顶背驰")
    if cs.get("last_signal") in ("S1", "S2", "S3"):
        s -= 0.02; parts.append(cs["last_signal"])
    if cs.get("last_signal") in ("B1", "B2", "B3"):
        s += 0.02; parts.append(cs["last_signal"])
    if cs.get("trend_ok"):
        s += 0.03; parts.append("笔向上")
    if cs.get("zone") == "upper":
        s += 0.02
    return {"score": round(ct.clamp(s, -0.08, 0.08), 3),
            "bustop": bool(cs.get("bustop")), "bi_dir": cs.get("bi_dir", "-"),
            "zone": cs.get("zone", "-"), "last_signal": cs.get("last_signal", "-"),
            "detail": "缠论:" + (",".join(parts) if parts else "中性")}


def score_all(ctx: dict) -> dict:
    """v4 打分：核心层(10因子五维) + 修正层(有界) + 缠论(结构) + 硬风控(8触发)。"""
    closes, amounts = ctx["closes"], ctx["amounts"]
    # 核心层：10 因子 → 维度合成（估值维权重保持 v4 定稿 0.10；
    # 经样本外寻优估值对创业板为负贡献，生产不注入 erp → value_erp=0 不干扰总分）。
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    core_series = cf.dimension_score(signals, _default_weights(0.10))
    core = {"score": round(core_series[-1], 3),
            "signals": {k: v[-1] for k, v in signals.items()}}
    snapshot = ctx["snapshot"] or {}
    mods = _dimension_modifier(snapshot, ctx)
    chan = _chan_signal(ctx)
    # 中际旭创双确认：指数信号 × 个股趋势/动量一致性（有界 ±0.10）
    stock_conf = {"score": 0.0, "agree": None, "detail": "个股数据不足，跳过"}
    sc = ctx.get("stock_ctx")
    if sc and sc.get("trend") and sc.get("mom"):
        idx_trend = {"score": float((core["signals"].get("trend_ma20_60") or 0.0))}
        stock_conf = ct.stock_confirm(sc["trend"], sc["mom"], idx_trend,
                                      sc.get("intraday_pct") or 0.0)
    else:
        stock_conf = {"score": 0.0, "agree": None, "detail": "个股数据不足，跳过"}
    mods["score"] = ct.clamp(mods["score"] + chan["score"] + stock_conf["score"],
                             -0.30, 0.30)
    mods["chan"] = chan
    mods["stock"] = stock_conf
    score = ct.clamp(core["score"] + mods["score"])
    glass = {"risk_off": str((snapshot.get("risk_state") or "")) == "risk_off",
             "basis_min_ap": None, "intraday_pct": ctx["intraday"],
             "overseas_drop": ctx.get("overseas_drop", 0.0)}
    aps = []
    for code in ("IC", "IM"):
        b = ((snapshot.get("basis") or {}) or {}).get(code) or {}
        try:
            aps.append(float(b.get("annual_pct")))
        except (TypeError, ValueError):
            pass
    glass["basis_min_ap"] = min(aps) if aps else None
    vol_pctile = None
    v = ((snapshot.get("vol") or {}) or {}).get("创业板指") or {}
    try:
        vol_pctile = float(v.get("pctile"))
    except (TypeError, ValueError):
        pass
    caps = cf.defensive_state(closes, vol_pctile, glass)
    # ERP 估值极端滤波：便宜度分位<0.10（=PE处于500日顶部10%，估值极贵）封顶6成。
    # ⚠ 语义纠正后实证为"净负贡献"（附注A：+291.9%基线 → +188.8%）：
    # 该约束并非已验证的正向超额，而是用户拍板保留的保守约束（泡沫期事前降仓）。
    # 勿误作"已验证正贡献"。
    _erp_series = ctx.get("erp_pctile") or []
    if _erp_series and _erp_series[-1] is not None and _erp_series[-1] < 0.10:
        caps["cap"] = min(caps["cap"], 0.6)
        caps.setdefault("triggers", []).append(
            f"估值极贵(便宜度{_erp_series[-1]:.0%})封顶6成")
    # 顶背驰：结构否决，封顶 6 成（带否决但不完全清仓）
    if chan["bustop"]:
        caps["cap"] = min(caps["cap"], 0.6)
        caps.setdefault("triggers", []).append("缠论顶背驰封顶6成")
    return {"core": core, "mods": mods, "score": score, "caps": caps}


# ---------------- 影子验证记录 ----------------

def update_shadow_history(state: dict, ctx: dict, today: str, score: float,
                          mods: dict, position: float) -> None:
    """补填历史多期前瞻 + 追加今日记录（各维分数 vs 1/3/5/10日前瞻 → 后续算 IC）。

    前瞻字段推进（字段存的是 index 偏移，计算时统一换算）：
      fwd3_off / fwd5_off / fwd10_off = 距完成记录日的交易日数，
      当前值 None=该期尚未来临；shadow_ic 据此换算实际前瞻收益。
    """
    hist = state.setdefault("history", [])
    closes, dates = ctx["closes"], ctx["dates"]
    idx = {d: i for i, d in enumerate(dates)}
    for h in hist:
        if h.get("next_ret") is None:
            i = idx.get(str(h.get("date") or ""))
            if i is not None and i + 1 < len(closes) and dates[i] < today:
                h["next_ret"] = round(closes[i + 1] / closes[i] - 1.0, 4)
        # 多期前瞻：记录"还需等几根"递减，0 时用 close 前缀补实际收益
        for offk, rk, k in (("fwd3_off", "r3", 3), ("fwd5_off", "r5", 5),
                            ("fwd10_off", "r10", 10)):
            off = h.get(offk)
            if isinstance(off, (int, float)) and h.get(rk) is None:
                i = idx.get(str(h.get("date") or ""))
                if i is None:
                    continue
                move = int(off)
                if move <= 0 and i + k < len(closes) and dates[i] < today:
                    base = closes[i]
                    h[rk] = round(closes[i + k] / base - 1.0, 4)
                elif dates[i] < today:
                    h[offk] = move - 1  # 尚未到期，递减等待
    ovs_drop = ctx.get("overseas_drop") or 0.0
    stock_d = (mods.get("stock") or {}).get("score", 0.0) if mods else 0.0
    chan_d = (mods.get("chan") or {}).get("score", 0.0) if mods else 0.0
    hist.append({"date": today, "score": score,
                 "core": mods["core"]["score"] if mods else 0.0,
                 "basis": mods["basis"] if mods else 0.0,
                 "flow": mods["flow"] if mods else 0.0,
                 "mood": mods["mood"] if mods else 0.0,
                 "news": mods["news"] if mods else 0.0,
                 "chan": chan_d, "stock": stock_d,
                 "kospi": 0.0, "sox": min(ovs_drop, 0.0), "vix": 0.0, "a50": 0.0,
                 "position": position, "next_ret": None,
                 "fwd3_off": 3, "fwd5_off": 5, "fwd10_off": 10,
                 "r3": None, "r5": None, "r10": None})
    state["history"] = hist[-HISTORY_LIMIT:]


# ---------------- 报告 ----------------

def render_report(today: str, res: dict, ctx: dict, dec: dict, prev_pos: float) -> str:
    core, caps = res["core"], res["caps"]
    mods = res["mods"]
    pos = dec["position"]
    chg = dec["changed"]
    if chg and pos < prev_pos:
        act = f"减仓至 {pos:.0%}"
    elif chg:
        act = f"加仓至 {pos:.0%}"
    elif dec["note"] and "确认" in dec["note"][0]:
        act = f"维持 {prev_pos:.0%}（{dec['note'][0]}）"
    else:
        act = f"维持 {pos:.0%}（今日无操作）"

    lines = [f"【创业板仓位信号 {today[5:]} 14:45】", ""]
    lines.append(f"■ 建议：{act}")
    lines.append(f"■ 综合分 {res['score']:+.2f}（核心 {core['score']:+.2f} / "
                 f"修正 {res['score'] - core['score']:+.2f}）")
    sig = core["signals"]
    _fmt = lambda a, b: f"{a:+.2f}/{b:+.2f}"
    lines.append(f"  趋势{_fmt(sig['trend_ma20_60'], sig['trend_momentum_60'])}"
                 f"｜量价{_fmt(sig['volprice_quadrant'], sig['volprice_amihud'])}"
                 f"｜波动{_fmt(sig['vol_regime'], sig['vol_term'])}"
                 f"｜估值{sig['value_erp']:+.2f}"
                 f"｜落袋{_fmt(sig['pullback_52w'], sig['dd60'])}")
    lines.append(f"  贴水{mods['basis']:+.2f}｜资金{mods['flow']:+.2f}"
                 f"｜情绪{mods['mood']:+.2f}｜资讯{mods['news']:+.2f}"
                 f"｜缠论{mods['chan'].get('score', 0.0):+.2f}"
                 f"｜旭创{mods['stock'].get('score', 0.0):+.2f}")
    if (mods.get("stock") or {}).get("detail") and "跳过" not in (mods["stock"] or {}).get("detail", ""):
        lines.append(f"  旭创确认：{mods['stock']['detail']}")
    if (mods.get("chan") or {}).get("detail") and "中性" not in (mods["chan"] or {}).get("detail", ""):
        lines.append(f"  {mods['chan']['detail']}（{mods['chan']['bi_dir']}/{mods['chan']['zone']}）")
    od = ctx.get("overseas_drop") or 0.0
    if od <= -0.03:
        lines.append(f"■ 外围：SOX/纳指/标普 t-1 最差 {od:.1%}（外围大幅下杀）")
    lines.append(f"■ 盘中：创业板指 {ctx['intraday']:+.2f}%")
    dar = ctx.get("day_amount_ratio") or 0.0
    if dar:
        lines.append(f"■ 量能：今日累计量/昨量 {dar:.2f}（量价因子用昨日完整量）")
    if caps["triggers"]:
        lines.append("■ 硬风控：" + "；".join(caps["triggers"]))
    else:
        lines.append("■ 硬风控：无触发")
    if dec["note"] and not chg and "确认" not in dec["note"][0]:
        lines.append("■ " + "；".join(dec["note"]))
    lines.append("")
    lines.append("档位线：≥+0.40满仓｜≥-0.15九成｜≥-0.30战略六成底仓｜更低空仓")
    lines.append("升档需连续2日确认，降档当日生效；15:00 前下单有效。仅供参考。")
    return "\n".join(lines)


def push_report(content: str, title: str) -> bool:
    token = os.getenv("PUSHPLUS_TOKEN")
    webhook = os.getenv("WECOM_WEBHOOK")
    from src.tools.push import push_via_pushplus, push_via_wecom
    if token:
        r = push_via_pushplus(token, title, content)
        if r.get("code") == 200:
            return True
        logger.warning("PushPlus 失败: %s", r)
    if webhook:
        r = push_via_wecom(webhook, title, content)
        if r.get("errcode") == 0:
            return True
        logger.warning("企业微信失败: %s", r)
    logger.error("无可用推送后端或全部失败")
    return False


# ---------------- 回测（核心层） ----------------

def _default_weights(val_w: float) -> dict:
    """估值维权重从趋势维挪出，其余固定，总权重恒=1.0。
    val_w=0.10 时与 v4 定稿一致（趋势0.35 量价0.20 波动0.20 估值0.10 落袋0.15）。"""
    return {"趋势": 0.45 - val_w, "量价": 0.20, "波动": 0.20,
            "估值": val_w, "落袋": 0.15}


def backtest_metrics(df, fee: float = 0.0, pe_map: Optional[dict] = None,
                     val_span: int = 500, val_w: float = 0.10,
                     tiers: tuple = ct.TIERS, erp_cap: bool = False) -> dict:
    """v4 核心层历史回测数值（无前视），供 run_backtest 与寻优脚本共用同一口径。

    时序：信号日 d 用 ≤d-1 收盘（因子只用既往），仓位吃 d+1 收益（对齐 T+1）。
    实时修正层+缠论不可回测。估值维由 pe_map（创业板50 TTM PE）注入。"""
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    dates = df.index
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    n = len(closes)
    start = 60
    # ERP 估值极端滤波（独立硬过滤，与打分脱钩）：
    # pe_map 存在时算便宜度分位序列，但 erp_cap 分支才启用（core 打分仍传 None，
    # 维持估值维关闭——经寻优 ERP 负贡献故不进打分，仅作"估值极贵封顶"）。
    erp_series = None
    if pe_map:
        pe = ipe.align_pe_by_dates(pe_map, date_strs)
        pe = [0.0 if v is None else v for v in pe]
        erp_series = ipe.pe_to_cheap_pctile(pe, val_span)
        erp = None  # 打分层不使用估值维（估值维负贡献，保持 None）
    signals = cf.core_signals(closes, amounts, erp_pctile=erp)
    comp = cf.dimension_score(signals, _default_weights(val_w))
    prev = {"position": 0.0, "pending": None}
    nav, peak, navs = 1.0, 1.0, []
    switches = 0
    down_next10 = []
    daily_rets = []
    pos_sum = 0.0
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        cap = caps["cap"]
        if erp_cap and erp_series is not None and erp_series[d] is not None and \
                erp_series[d] < 0.10:
            # 便宜度<0.10=PE处于顶部10%（估值极贵）→封顶6成，与 score_all 同口径
            cap = min(cap, 0.6)
        dec = ct.decide_position(comp[d], cap, prev, tiers=tiers)
        if dec["changed"]:
            switches += 1
            nav *= (1 - fee * abs(dec["position"] - prev["position"]))
            if dec["direction"] == "down" and d + 11 < n:
                down_next10.append(closes[d + 10] / closes[d] - 1.0)
        prev = {"position": dec["position"], "pending": dec["pending"]}
        pos_sum += dec["position"]
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        daily_rets.append(dec["position"] * r)
        peak = max(peak, nav)
        navs.append(nav)
    total = nav - 1.0
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1 if years > 0 else 0.0
    mdd = min((v / max(navs[:i + 1]) - 1.0) if i else 0.0 for i, v in enumerate(navs)) \
        if navs else 0.0
    mu = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
    sd = (sum((x - mu) ** 2 for x in daily_rets) / max(1, len(daily_rets) - 1)) ** 0.5
    sharpe = mu / sd * (244 ** 0.5) if sd > 0 else 0.0
    bh = closes[-1] / closes[start] - 1.0
    bh_navs = [closes[i + 1] / closes[start] for i in range(start, n - 1)]
    bh_mdd = min(v / max(bh_navs[:i + 1]) - 1.0 for i, v in enumerate(bh_navs)) \
        if bh_navs else 0.0
    calmar = cagr / abs(mdd) if mdd else 0.0
    calmar_b = (closes[-1] / closes[start]) ** (1 / max(1, years)) - 1
    calmar_b = calmar_b / abs(bh_mdd) if bh_mdd else 0.0
    dodge = (sum(1 for x in down_next10 if x < 0) / len(down_next10),
             sum(down_next10) / len(down_next10)) if down_next10 else (0.0, 0.0)
    return {"dates": dates, "start": start, "total": total, "cagr": cagr,
            "sharpe": sharpe, "mdd": mdd, "calmar": calmar,
            "bh": bh, "bh_mdd": bh_mdd, "calmar_b": calmar_b,
            "switches": switches, "avg_pos": pos_sum / max(1, len(navs)),
            "n_navs": len(navs), "n_down": len(down_next10),
            "down_dodge": dodge, "has_val": erp_series is not None}


def run_backtest(df, fee: float = 0.0, pe_map: Optional[dict] = None,
                 val_span: int = 500, val_w: float = 0.10,
                 tiers: tuple = ct.TIERS, erp_cap: bool = False) -> str:
    m = backtest_metrics(df, fee, pe_map, val_span, val_w, tiers, erp_cap)
    val_note = (f"估值：创业板50 TTM PE 滚动{val_span}日分位，仅作极端滤波"
                f"(便宜度<0.1=PE顶部10% 封顶6成，erp_cap)"
                if m["has_val"] else "估值源缺失（估值滤波关闭）")
    ds, s = m["dates"], m["start"]
    dodge = m["down_dodge"]
    return "\n".join([
        f"创业板仓位信号·v4核心层回测（{ds[s].date()} ~ {ds[-1].date()}，"
        f"{m['n_navs']}个交易日，成本{fee:.1%}/次）", "",
        f"策略累计 {m['total']:+.1%} / 年化 {m['cagr']:+.1%} / 夏普 {m['sharpe']:.2f} / "
        f"最大回撤 {m['mdd']:.1%} / 卡玛 {m['calmar']:.2f}",
        f"买入持有  {m['bh']:+.1%} / 最大回撤 {m['bh_mdd']:.1%} / 卡玛 {m['calmar_b']:.2f}",
        f"换仓 {m['switches']} 次｜平均仓位 {m['avg_pos']:.0%}",
        "",
        f"降档质量：{m['n_down']} 次减仓后10日市场 {dodge[1]:+.1%}（均值），"
        f"{dodge[0]:.0%} 段为下跌",
        "口径：信号日 d 用当日 d 收盘价出信号、吃 d+1 收益（收盘后决策对齐场外基金T+1）；"
        "仅核心层10因子五维，实时修正层+缠论不可回测（影子期再评估）。" + val_note + "。",
    ])


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="创业板仓位信号（CPO/PCB 场外基金手动执行版）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送不写状态")
    ap.add_argument("--push", action="store_true", help="推送 + 写状态（云端定时）")
    ap.add_argument("--backtest", action="store_true", help="核心层历史回测")
    ap.add_argument("--shadow", action="store_true", help="影子期因子IC报告")
    args = ap.parse_args()

    # 历史源：新浪全量（12年，绕代理）优先，东财增量链回退
    df = load_index_sina(SYMBOL)
    if df is None or df.empty:
        df = load_index_daily_full(SYMBOL, "20200101")
    if df is None or df.empty:
        raise SystemExit("创业板指日线获取失败，退出（不推送无数据信号）")
    logger.info("399006 日线 %d 根（%s ~ %s）", len(df),
                df.index[0].date(), df.index[-1].date())

    if args.shadow and not args.push:
        # 纯 --shadow 报告模式。--push --shadow（云端组合）不在此短路，
        # 否则推送逻辑永远执行不到（曾致云端 14:45 从不推送/状态不写/影子不积累）。
        state = load_state()
        hist = state.get("history") or []
        if not hist:
            print("影子期尚无样本记录（需在 --push 模式下运行积累）")
            return
        ic = ct.shadow_ic(hist)
        print("创业板择时·影子期因子 IC（Spearman vs 1/3/5/10日前瞻）")
        for f, info in ic.items():
            parts = []
            for hl, hr in (("1", "h1"), ("3", "h3"), ("5", "h5"), ("10", "h10")):
                e = info.get(hr) or {}
                tag = "样本不足" if e.get("ic") is None else f"{e['ic']:+.4f}"
                eff = "" if e.get("ic") is None \
                    else ("✓" if abs(e["ic"]) >= 0.05 else "×")
                parts.append(f"{hl}日:{tag}{eff}")
            print(f"  {f:<8} " + "  ".join(parts))
        return

    if args.backtest:
        # ERP 估值极端滤波（便宜度<0.1=PE顶部10% 封顶6成）：估值极贵时降仓。
        # 估值维不进打分（erp_cap 独立硬过滤，core 仍 erp_pctile=None）。
        print(run_backtest(df, pe_map=ipe.load_cy50_pe(PROJECT_ROOT),
                           erp_cap=True))
        return

    today = datetime.now().strftime("%Y-%m-%d")
    ctx = gather_context(df)
    res = score_all(ctx)

    state = load_state()
    if args.push and str(state.get("last_date") or "") == today:
        logger.warning("今日已推送过（last_date=%s），去重跳过", today)
        return
    prev_pos = float(state.get("position") or 0.0)
    prev = {"position": prev_pos, "pending": state.get("pending")}
    dec = ct.decide_position(res["score"], res["caps"]["cap"], prev)

    report = render_report(today, res, ctx, dec, prev_pos)
    print("\n" + report + "\n")

    if args.push:
        if push_report(report, f"创业板仓位信号 {today[5:]}"):
            state.update({"last_date": today, "position": dec["position"],
                          "pending": dec["pending"], "last_score": res["score"]})
            update_shadow_history(state, ctx, today, res["score"], res, dec["position"])
            save_state(state)
            logger.info("已推送并写状态（仓位 %.0f）", dec["position"])
        else:
            logger.warning("推送失败，状态不更新（明日重跑）")
    elif args.dry_run:
        print("（dry-run：不推送不写状态）")


if __name__ == "__main__":
    main()
