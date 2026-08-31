# -*- coding: utf-8 -*-
"""
创业板仓位信号入口（run_chinext_timing.py）
====================================================
每个交易日 14:45（北京）云端触发，推送创业板方向（CPO/PCB 场外基金）
的目标仓位建议，用户 15:00 截单前手动执行。

数据链（全部带降级，增强维度缺失时缩一维；核心历史不足则停止推送）：
  399006 日线(新浪全量 → 东财/腾讯免费回退) + 盘中实时(腾讯)
  + factor_state 快照（贴水/资金流/涨停情绪/宽度/波动分位/risk_state）
  + citic_pos_state（中信全合约净持仓）
  + 当日资讯候选（已推送强档 ∪ 预筛候选，LLM 方向 × 科技相关度）

用法：
  python scripts/run_chinext_timing.py --dry-run    # 本地打印
  python scripts/run_chinext_timing.py --push       # 推送 + 写状态（云端）
  python scripts/run_chinext_timing.py --push --force  # 忽略同日去重，强推（复验用，如周六）
  python scripts/run_chinext_timing.py --backtest   # 核心层历史回测验证
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

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
from src.strategy.data_freshness import BJT, _is_workday  # noqa: E402
from src.strategy.state_io import (atomic_write_json, get_gist_config,  # noqa: E402
                                   patch_gist_file)

TIMING_STATE_FILENAME = "chinext_timing_state.json"
_LOCAL_STATE_PATH = PROJECT_ROOT / "logs" / TIMING_STATE_FILENAME
SYMBOL = "399006"
SYMBOL_STOCK = "300308"  # 中际旭创（科技龙头情绪标的，双确认个股侧）
HISTORY_LIMIT = 120  # 影子 IC 记录条数上限
MIN_SIGNAL_HISTORY = 62  # 60 日 warmup + 至少 1 根可用于 T+1 收益的完整日线


def _load_local_env() -> None:
    """加载本地运行配置；已有进程环境变量优先于项目 .ENV。"""
    load_dotenv(PROJECT_ROOT / ".ENV", override=False)


def _latest_valid_citic_position(state: dict, today=None):
    """从中信历史中选最近一个有效交易日记录，兼容历史无序。"""
    hist = (state or {}).get("pos_history") or []
    if not isinstance(hist, list):
        return None
    today = today or datetime.now(BJT).date()
    candidates = []
    for item in hist:
        if not isinstance(item, dict):
            continue
        day = str(item.get("day") or "")[:10]
        try:
            parsed = pd.Timestamp(day).date()
        except (TypeError, ValueError):
            continue
        if parsed > today:
            continue
        # 日报允许使用上一交易日；周末/节假日由公共新鲜度规则处理。
        from src.strategy.data_freshness import is_recent_data_date
        if is_recent_data_date(parsed, max_lag_days=1, calendar="cn", today=today):
            candidates.append((parsed, item))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


# ---------------- 状态持久化（Gist 单写端：本 workflow） ----------------

def load_state() -> dict:
    """加载择时状态：配置 Gist 时严格读取，未配置时读取本地文件。"""
    token, gist_id = get_gist_config()
    if token and gist_id:
        # 该状态由本 workflow 写回；云端读取异常时禁止用本地旧状态去重后
        # 再覆盖 Gist。Gist 成功但文件不存在表示首次部署，返回空状态即可。
        return nl._read_gist_file(TIMING_STATE_FILENAME, strict=True)
    try:
        state = json.loads(_LOCAL_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> bool:
    """写本地 + 尽力写 Gist（云端跨容器持久化）。"""
    token, gist_id = get_gist_config()
    try:
        atomic_write_json(_LOCAL_STATE_PATH, state, indent=1)
    except OSError as e:
        logger.warning("本地状态写入失败: %s", e)
        return False
    if not (token and gist_id):
        return True  # 本地已写，无 Gist 配置（本地模式）
    # 2026-08-31 修复：If-Match 乐观锁被 GitHub 拒绝（Gists API 不支持
    # 条件请求，400），曾致云端零写入。并发安全由 concurrency 串行 +
    # 单文件提交保证；影子 history 的防覆盖依赖同 group 串行。
    try:
        patch_gist_file(
            TIMING_STATE_FILENAME,
            json.dumps(state, ensure_ascii=False, indent=1),
            token, gist_id,
        )
        return True
    except Exception as e:
        # 本地已保底写入，云端失败不阻断本次运行（保持原降级语义）
        logger.warning("Gist 状态写入失败（本地已保底）: %s", e)
        return False


# ---------------- 数据聚合 ----------------

def gather_context(df) -> dict:
    """聚合全部数据源，任何一源失败独立降级。

    数据口径（2026-08-28 v5.1 用户拍板）：末根若为当日（14:45 盘中 partial），
    保留作为当日快照——核心层均线/动量/量价直接用"今天到现在的走势"决策，
    不用 d-1 收盘（对当日加减仓更有意义）。14:45→15:00 收盘的 15 分钟价差接受为近似。
    - 核心层 closes/amounts/highs/lows 含当日 14:45 快照（末根=当日 partial）；
    - 当日盘中信息仍走 intraday（指数涨跌幅）进入修正层/硬风控；
    - 影子 next_ret 回填在次日运行时用 closes[i+1]/closes[i]（末根为当日快照价），
      属"快照到快照"的近似信号评估口径，非完整收盘收益。
    """
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    # 缠论需 high/low（新浪全量链提供；备用链缺则退化跳过缠论）
    highs = (df["high"].tolist() if "high" in df else [])
    lows = (df["low"].tolist() if "low" in df else [])
    today_s = datetime.now(BJT).strftime("%Y-%m-%d")

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
    # 修正层快照时效兜底（P0-20260824）：快照 ts 非今日=采集停更，
    # 修正分基于旧快照已失真 → 推送标注告知 + 影子 raw 记 None 防污染验门。
    # 不擅自改打分（避免行为翻转），仅显式暴露数据时效供人工判断。
    snapshot_ts = (snapshot.get("ts") or "") if snapshot else ""
    snapshot_stale = _snapshot_is_stale(snapshot, today_s)
    try:
        cs = nl.load_citic_pos_state()
        latest = _latest_valid_citic_position(cs, today=datetime.now(BJT).date())
        if latest:
            citic_net = float((latest.get("net") or {}).get("_total") or 0.0)
            citic_day = str(latest.get("day") or "")
    except Exception as e:
        logger.warning("citic 状态读取失败: %s", type(e).__name__)
    try:
        rt = nl.load_realtime_state()
        today = datetime.now(BJT).strftime("%Y-%m-%d")
        # 资讯输入覆盖全部重大候选：已推送强档 ∪ 当日预筛候选（P7-1）。
        # 收敛点：资讯维度只看当日真实候选（含方向），不看历史事件。
        events = nl.today_news_events(rt, today)
    except Exception as e:
        logger.warning("资讯事件读取失败: %s", type(e).__name__)

    # 外盘 t-1 隔夜跌幅（硬风控盘中急跌的同源确认，缺源降级为 0 不阻断）
    overseas_drop = 0.0
    try:
        ov = ovs.load_overseas(PROJECT_ROOT)
        overseas_drop = ovs.overnight_drop(ov, datetime.now(BJT))
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
            today_s = datetime.now(BJT).strftime("%Y-%m-%d")
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

    # 当日实时量能单独记录 day_amount_ratio 供盘面判断，不进量价因子分位。
    # （末根若为当日 14:45 partial，则 amounts[-1]=当日累计量、amounts[-2]=昨日完整量，
    #   两者比值为当日量能进度参考；缓存命中（末根为昨日完整量）时 ratio 语义不存在，置 0）
    day_amount_ratio = 0.0
    if dates and dates[-1] == today_s:
        _amt_raw = (df["amount"].tolist() if "amount" in df else [])
        if len(_amt_raw) >= 2 and _amt_raw[-1] and _amt_raw[-2]:
            day_amount_ratio = _amt_raw[-1] / _amt_raw[-2]
    return {"closes": closes, "amounts": amounts, "dates": dates,
            "highs": highs, "lows": lows,
            "history_bars": len(closes),
            "history_last_date": dates[-1] if dates else "",
            "intraday": intraday, "snapshot": snapshot,
            "snapshot_ts": snapshot_ts, "snapshot_stale": snapshot_stale,
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
        return ipe.pe_to_cheap_pctile(pe, VAL_SPAN)
    except Exception as e:
        logger.warning("估值分位计算失败（降级为0）: %s", type(e).__name__)
        return None


def _dimension_modifier(snapshot: dict, ctx: dict) -> dict:
    """v4 修正层（有界 ±0.30）：派生品贴水 + 资金 + 情绪 + 资讯 + 实时(外盘4评分占位)。
    已按用户要求移除中信净持仓。外盘因子(KOSPI/SOX/VIX/A50)数据源在 Phase C/D 接入，
    接入前该子项恒 0；贴水/资金/情绪/资讯已有 factor_state 源，实时生效。"""
    # 快照有明确日期且不是今天时，所有依赖该快照的增强项一起失效。
    # 不能只在报告中提示，否则旧基差/资金/情绪仍会改变当日仓位。
    if ctx.get("snapshot_stale"):
        return {"score": 0.0, "basis": 0.0, "flow": 0.0,
                "mood": 0.0, "news": 0.0, "detail": "修正层快照过期，增强项降级"}
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
    f_score = round(ct.clamp(f_score, -0.05, 0.05), 3)
    # 情绪 ±0.04
    m_score = ct.mood_modifier(
        (snapshot.get("sentiment") or {}) if snapshot else {},
        (snapshot.get("breadth") or {}) if snapshot else {},
        (snapshot.get("option") or {}) if snapshot else None)["score"]
    m_score = round(ct.clamp(m_score, -0.04, 0.04), 3)
    # 资讯 ±0.06
    dir_sign = {d: sign for d, (_lbl, sign) in nl._DIR_LABEL.items()}
    n_score = ct.news_modifier(ctx["events"], dir_sign)["score"]
    n_score = round(ct.clamp(n_score, -0.06, 0.06), 3)
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
    bustop = bool(cs.get("bustop"))
    if bustop:
        s -= 0.06; parts.append("顶背驰")
    if cs.get("last_signal") in ("S1", "S2", "S3"):
        s -= 0.02; parts.append(cs["last_signal"])
    if cs.get("last_signal") in ("B1", "B2", "B3"):
        # 顶背驰优先：买点候选降级为观察，不再贡献正分（2026-08-27 实证：
        # 顶背驰+B2+笔向上+upper 净 +0.01——同源买侧代理分抵消否决级 -0.06）
        if bustop:
            parts.append(f"{cs['last_signal']}降级观察")
        else:
            s += 0.02; parts.append(cs["last_signal"])
    if cs.get("trend_ok"):
        s += 0.03; parts.append("笔向上")
    if cs.get("zone") == "upper":
        s += 0.02
    if bustop:
        # 否决保底：买侧代理分（笔向上/upper）不得把顶背驰拉回正值
        s = min(s, -0.04)
    return {"score": round(ct.clamp(s, -0.08, 0.08), 3),
            "bustop": bool(cs.get("bustop")), "bi_dir": cs.get("bi_dir", "-"),
            "zone": cs.get("zone", "-"), "last_signal": cs.get("last_signal", "-"),
            "detail": "缠论:" + (",".join(parts) if parts else "中性")}


def score_all(ctx: dict) -> dict:
    """v5 打分：核心层(9个注册因子、8个有效因子、五维) + 修正层(有界) + 缠论(结构) + 硬风控。"""
    closes, amounts = ctx["closes"], ctx["amounts"]
    # 核心层：9 个注册因子 → 五维合成（v5 权重 T.50：趋势.50/量价.20/波动.20/估值0/落袋.10；
    # 估值维打分关闭，value_erp 不注入 → 恒为 0 不干扰总分）。
    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    core_series = cf.dimension_score(signals, _default_weights(0.10))
    core = {"score": round(core_series[-1], 3),
            "signals": {k: v[-1] for k, v in signals.items()}}
    snapshot = {} if ctx.get("snapshot_stale") else (ctx["snapshot"] or {})
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
    # 历史宽松信息集下该约束曾表现为净负贡献（旧对照：+291.9% → +188.8%）。
    # 当前严格回测结果以 backtest_metrics 的 d-1 信息集为准，不能把旧对照当作生产基线。
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
                          mods: dict, position: float,
                          prev_pos: float = None) -> None:
    """补填历史多期前瞻 + 追加今日记录（各维分数 vs 1/3/5/10日前瞻 → 后续算 IC）。

    前瞻字段推进（字段存的是 index 偏移，计算时统一换算）：
      fwd3_off / fwd5_off / fwd10_off = 距完成记录日的交易日数，
      当前值 None=该期尚未来临；shadow_ic 据此换算实际前瞻收益。
    prev_pos（2026-08-27 审计扩展）：决策前仓位，与 position 组成仓位轨迹；
    缺省 None 不影响旧调用（兼容测试旧签名）。
    """
    # 兼容两种入参：res 顶层（main() 传入）或 res["mods"]：basis/flow/mood/news/chan/stock
    # 在 mods 子字典，core 在 res 顶层（score_all 返回 res={core,mods,score,caps}）。
    # 曾因 ve 直接用 mods["basis"] 而 main 传 res 导致 KeyError: 'basis'（周一首推必炸），
    # 且 stock/chan 取顶层恒 None → 恒 0（假数据）。统一解构修复。
    res = mods  # main 传入的是 res（score_all 的完整返回）
    m = res.get("mods") or res
    core_d = res.get("core") or m.get("core") or {}
    core_s = core_d.get("score", 0.0)
    sig_all = core_d.get("signals") or {}
    caps_d = res.get("caps") or m.get("caps") or {}
    chan_d_dict = m.get("chan") or {}
    ovs_drop = ctx.get("overseas_drop") or 0.0
    stock_d = (m.get("stock") or {}).get("score", 0.0)
    chan_d = chan_d_dict.get("score", 0.0)
    hist = state.setdefault("history", [])
    closes, dates = ctx["closes"], ctx["dates"]
    idx = {d: i for i, d in enumerate(dates)}
    for h in hist:
        if h.get("next_ret") is None:
            i = idx.get(str(h.get("date") or ""))
            if i is not None and i + 1 < len(closes) and dates[i] < today:
                h["next_ret"] = round(closes[i + 1] / closes[i] - 1.0, 4)
        # 回填当日收盘涨幅（P1-3 执行滑点，2026-08-29）：与 14:45 的 intraday_pct
        # 配对，二者之差即"信号→成交"的 15 分钟价差。当日 14:45 运行时拿到的是
        # 盘中快照，收盘价要等次日才能取到完整值，故在这里回填（与 next_ret 同机制）。
        if h.get("close_pct") is None:
            i = idx.get(str(h.get("date") or ""))
            if i is not None and i > 0 and dates[i] < today:
                h["close_pct"] = round(closes[i] / closes[i - 1] - 1.0, 4)
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
    # 净值埋点（2026-08-29 P1-2）：截至本条之前的策略/基准累计净值
    _nav, _bh_nav = _cumulative_nav(hist)
    hist.append({"date": today, "score": score, "core": core_s,
                 "basis": m.get("basis", 0.0), "flow": m.get("flow", 0.0),
                 "mood": m.get("mood", 0.0), "news": m.get("news", 0.0),
                 "chan": chan_d, "stock": stock_d,
                 "kospi": 0.0, "sox": min(ovs_drop, 0.0), "vix": 0.0, "a50": 0.0,
                 "position": position, "next_ret": None,
                 "nav": _nav, "bh_nav": _bh_nav,
                 "fwd3_off": 3, "fwd5_off": 5, "fwd10_off": 10,
                 "r3": None, "r5": None, "r10": None,
                 # 原始输入（验门增益：离散档分对 Spearman IC 分辨力弱，补原始量可直接做
                 # 原始 IC / 分层 IC，不依赖档位；snapshot 缺失的指标记 None）
                 "raw": _shadow_raw(ctx),
                 # 审计扩展（2026-08-27）：回答"信号对不对/版本对不对/风控有没有效"——
                 # ① commit 溯源（分清信号问题 vs 远端未部署）② prev→final 仓位轨迹
                 # ③ 盘中涨幅（"低分但盘中大涨"后 N 日表现的提问依据）④ 风控状态与
                 # 触发项（深回撤区空仓是保护还是错过）⑤ 缠论结构原始状态（顶背驰后
                 # 1/3/5/10 日验证）⑥ 核心五维主信号值（分维度归因，不用只看总 core）
                 "commit": _git_commit(), "prev_pos": prev_pos,
                 "intraday_pct": ctx.get("intraday"),
                 "cap": caps_d.get("cap"), "cap_triggers": caps_d.get("triggers") or [],
                 "chan_bustop": bool(chan_d_dict.get("bustop")),
                 "chan_last_signal": chan_d_dict.get("last_signal"),
                 "sig": {k: sig_all.get(k) for k in
                         ("trend_ma20_60", "volprice_quadrant", "vol_regime",
                          "pullback_52w", "dd60")},
                 "probe": _shadow_probes(ctx, res)})
    state["history"] = hist[-HISTORY_LIMIT:]


def _shadow_probes(ctx: dict, res: dict) -> dict:
    """影子规则候选探针（2026-08-27 P3）：只记录，不改仓位，不改分数。

    三个候选规则在改真实仓位线之前必须先积累样本验证（|IC|≥0.05 且 ≥10 样本）：
    ① rebound：深回撤区 + 盘中涨幅>1.5% + 量价主信号为正 + 无顶背驰——
       "低分空仓但盘中大涨"的反弹确认探针（当前系统对深回撤后强反弹是否过于迟钝）。
    ② low_repair：深回撤区 + 收盘站回5日线——把"深回撤"拆成"下跌延续 vs 低位修复"
       两态的探针（低位修复期深回撤可能不再是看空信号）。
    ③ bustop 否决统计由已有 chan_bustop 字段承载（顶背驰后 1/3/5/10 日 vs 非顶背驰组）。
    布尔标记 × 已有 next_ret/r3/r5/r10 → 事后分两组对比前瞻收益即可，零侵入。
    """
    closes = ctx.get("closes") or []
    intraday = float(ctx.get("intraday") or 0.0)
    sig = ((res.get("core") or {}).get("signals") or {})
    bustop = bool(((res.get("mods") or {}).get("chan") or {}).get("bustop"))
    deep_dd = False
    if len(closes) >= 60:
        dd = closes[-1] / max(closes[-60:]) - 1.0
        deep_dd = dd <= -0.12
    ma5 = (sum(closes[-5:]) / 5.0) if len(closes) >= 5 else None
    try:
        vp = float(sig.get("volprice_quadrant") or 0.0)
    except (TypeError, ValueError):
        vp = 0.0
    return {"deep_dd": deep_dd,
            "rebound": bool(deep_dd and intraday > 1.5 and vp > 0 and not bustop),
            "low_repair": bool(deep_dd and ma5 is not None and closes[-1] > ma5)}


def _shadow_raw(ctx: dict) -> dict:
    """提取修正层原始输入（供分层/原始IC验门）：worst_ap/main_net/down_pct/pcr 等。
    缺源全部置 None（与修正层"缺源降级0"解耦，避免假0污染验门）。
    快照停更（stale）时整体置 None：原始值非当日=假样本，污染影子 IC。"""
    if ctx.get("snapshot_stale"):
        return {"basis_min_ap": None, "main_net": None, "down_pct": None, "pcr": None}
    snap = ctx.get("snapshot") or {}
    # 贴水：IC/IM 最差年化基差（原始，非档位分）
    _aps = []
    for _code in ("IC", "IM"):
        _b = ((snap.get("basis") or {}) or {}).get(_code) or {}
        try:
            _aps.append(float(_b.get("annual_pct")))
        except (TypeError, ValueError):
            pass
    # 资金：两市主力净流。快照键为 main_net_yi（与 factor_collector.build_snapshot
    # 全链路一致）；曾误读 main_net 致资金流原始值恒 None，资金维原始 IC 验门静默失效。
    _mn = None
    _f = snap.get("flows")
    if isinstance(_f, dict):
        _mn = _f.get("main_net_yi")
        try:
            _mn = float(_mn) if _mn is not None else None
        except (TypeError, ValueError):
            _mn = None
    # 情绪：下跌占比
    _dp = None
    _b = snap.get("breadth")
    if isinstance(_b, dict) and _b.get("down_pct") is not None:
        try:
            _dp = float(_b["down_pct"])
        except (TypeError, ValueError):
            _dp = None
    # 期权：PCR
    _pcr = None
    _o = snap.get("option")
    if isinstance(_o, dict) and _o.get("pcr") is not None:
        try:
            _pcr = float(_o["pcr"])
        except (TypeError, ValueError):
            _pcr = None
    return {"basis_min_ap": min(_aps) if _aps else None,
            "main_net": _mn, "down_pct": _dp, "pcr": _pcr}


# ---------------- 报告 ----------------

def _git_commit() -> str:
    """当前代码版本（推送报告溯源：分清"信号问题"还是"远端未部署"）。"""
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=str(PROJECT_ROOT))
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _cumulative_nav(hist: list) -> tuple:
    """基于影子 history 的 next_ret 累乘策略净值与基准净值（均从 1.0 起）。

    策略净值 = Π(1 + position × next_ret)；基准净值 = Π(1 + next_ret)
    （基准 = 满仓持有创业板指，与回测口径一致；未回填 next_ret 的记录跳过）

    用途（2026-08-29 P1-2）：净值埋点是策略失效熔断、绩效归因、执行对账的
    共同基础——从埋点日起累积，样本足够后即可量化"策略 vs 买入持有"。
    """
    nav = bh = 1.0
    for r in hist:
        nr = r.get("next_ret")
        if not isinstance(nr, (int, float)):
            continue
        bh *= (1.0 + nr)
        nav *= (1.0 + (r.get("position") or 0.0) * nr)
    return round(nav, 6), round(bh, 6)


def strategy_health(hist: list, window: int = 20,
                    lag_gate: float = 0.10, dd_gate: float = 0.15) -> dict:
    """策略健康度（P1-2 策略失效熔断，2026-08-29）。

    **只监控告警，不改仓位**——保持决策透明，是否干预由人判断。
    三个条件：
      ① 滚动 window 日策略累计落后基准 ≥ lag_gate（策略衰减/系统性踏空）
      ② 策略滚动净值回撤 ≥ dd_gate（回撤失控）
      ③ 有效样本 < window → 不告警（安全默认，避免小样本误报）

    Returns:
        {"ok": bool, "level": "ok"|"alert"|"insufficient",
         "reasons": [str], "stats": dict}
    """
    recs = [r for r in hist if isinstance(r.get("next_ret"), (int, float))]
    if len(recs) < window:
        return {"ok": True, "level": "insufficient",
                "reasons": [f"有效样本 {len(recs)}/{window}，不触发熔断（安全默认）"],
                "stats": {"n": len(recs), "window": window}}
    win = recs[-window:]
    nav = bh = peak = 1.0
    mdd = 0.0
    for r in win:
        nr = r.get("next_ret")
        nav *= (1.0 + (r.get("position") or 0.0) * nr)
        bh *= (1.0 + nr)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    lag = bh - nav  # 正数 = 策略落后基准
    reasons = []
    if lag >= lag_gate:
        reasons.append(
            f"近{window}日策略落后基准 {lag * 100:.1f}pp"
            f"（策略 {nav - 1:+.1%} vs 持有 {bh - 1:+.1%}）")
    if -mdd >= dd_gate:
        reasons.append(
            f"近{window}日策略回撤 {mdd * 100:.1f}% ≥ 阈值 {dd_gate * 100:.0f}%")
    return {"ok": not reasons, "level": "alert" if reasons else "ok",
            "reasons": reasons,
            "stats": {"n": len(recs), "window": window, "nav": round(nav, 4),
                      "bh": round(bh, 4), "lag": round(lag, 4),
                      "mdd": round(mdd, 4)}}


def execution_slippage(hist: list) -> dict:
    """执行滑点测算（P1-3，2026-08-29）。

    实盘链路：14:45 出信号（基于盘中价）→ 15:00 前下单 → 按**当日收盘净值**成交。
    因此"信号价"与"成交价"天然存在 15 分钟价差，这是真实摩擦成本：

        slip = 当日收盘涨幅 − 14:45 盘中涨幅

    只有**仓位变化**（换仓）才产生实际摩擦：成本 = |Δposition| × slip
    （slip>0 表示 14:45 后继续上涨，买入方吃亏、卖出方少赚）

    量纲注意：``intraday_pct`` 存的是**百分点**（如 -1.23 = -1.23%），
    而 ``close_pct`` / ``next_ret`` 是**小数**（如 -0.0123），此处统一为小数。

    Returns:
        {"n": 有效样本, "mean_slip": 平均滑点, "worst": 最差单笔,
         "total_cost": 累计摩擦（按换仓幅度加权）, "events": [...]}
    """
    events = []
    prev_pos = None
    for r in hist:
        pos = r.get("position")
        ip, cp = r.get("intraday_pct"), r.get("close_pct")
        if not isinstance(ip, (int, float)) or not isinstance(cp, (int, float)):
            if isinstance(pos, (int, float)):
                prev_pos = pos
            continue
        slip = cp - ip / 100.0   # 百分点 → 小数
        dpos = abs(float(pos) - prev_pos) if isinstance(pos, (int, float)) \
            and prev_pos is not None else 0.0
        events.append({"date": r.get("date"), "slip": round(slip, 6),
                       "dpos": round(dpos, 4),
                       "cost": round(dpos * slip, 6),
                       "intraday_pct": ip, "close_pct": cp})
        if isinstance(pos, (int, float)):
            prev_pos = pos
    if not events:
        return {"n": 0, "mean_slip": 0.0, "mean_abs_slip": 0.0, "worst": 0.0,
                "total_cost": 0.0, "events": []}
    slips = [e["slip"] for e in events]
    return {"n": len(events),
            "mean_slip": round(sum(slips) / len(slips), 6),
            "mean_abs_slip": round(sum(abs(s) for s in slips) / len(slips), 6),
            "worst": round(max(slips, key=abs), 6),
            "total_cost": round(sum(e["cost"] for e in events), 6),
            "events": events}


def render_report(today: str, res: dict, ctx: dict, dec: dict, prev_pos: float,
                  health: dict | None = None) -> str:
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

    now_bj = datetime.now(BJT)
    lines = [f"【创业板仓位信号 {today[5:]} {now_bj:%H:%M}】", ""]
    # 档位基准：综合分按 TIERS 本应到几成（复用现有映射，避免两套口径漂移）
    tier_target = ct.score_to_tier(float(res["score"]), ct.TIERS)
    sugg = f"■ 建议：{act}"
    # 仅当纯档位映射越过风控上限时注记（排除升档待确认造成的差距，避免误标）
    if tier_target > float(caps.get("cap") or 1.0):
        sugg += f"（档位基准 {tier_target:.0%}，受硬风控封顶→ {pos:.0%}）"
    lines.append(sugg)
    lines.append(f"■ 综合分 {res['score']:+.2f} ＝ 核心 {core['score']:+.2f} ＋ "
                 f"修正 {res['score'] - core['score']:+.2f}")
    sig = core["signals"]
    _fmt = lambda a, b: f"{a:+.2f}/{b:+.2f}"
    lines.append("  核心："
                 f"趋势{_fmt(sig['trend_ma20_60'], sig['trend_momentum_60'])}"
                 f"｜量价{_fmt(sig['volprice_quadrant'], sig['volprice_amihud'])}"
                 f"｜波动{_fmt(sig['vol_regime'], sig['vol_term'])}"
                 f"｜估值{sig['value_erp']:+.2f}"
                 f"｜落袋{_fmt(sig['pullback_52w'], sig['dd60'])}")
    # 旭创从修正行移除，改由下方"旭创确认"行承载（避免 -0.10 与明细 -0.14 重复且打架）
    # 数据健康度（2026-08-27）：快照源缺失时该子项降级 0，+0.00 无法区分"真实中性"
    # vs"没数据"——源头缺失标注(缺)。快照整体停更由下方 ⚠ 行告警覆盖，不重复标。
    raw_h = None if ctx.get("snapshot_stale") else _shadow_raw(ctx)

    def _h(label: str, val, key: str) -> str:
        tag = "(缺)" if (raw_h is not None and raw_h.get(key) is None) else ""
        return f"{label}{val:+.2f}{tag}"

    lines.append("  修正："
                 f"{_h('贴水', mods['basis'], 'basis_min_ap')}"
                 f"｜{_h('资金', mods['flow'], 'main_net')}"
                 f"｜{_h('情绪', mods['mood'], 'down_pct')}"
                 f"｜资讯{mods['news']:+.2f}"
                 f"｜缠论{mods['chan'].get('score', 0.0):+.2f}")
    lines.append("  注：X/Y＝该因子主信号/副信号（双口径），单值因子仅取主信号。")
    stock_detail = (mods.get("stock") or {}).get("detail", "")
    if stock_detail and "跳过" not in stock_detail:
        net = (mods.get("stock") or {}).get("score", 0.0)
        lines.append(f"  旭创确认（净{net:+.2f}）：{stock_detail}")
    if (mods.get("chan") or {}).get("detail") and "中性" not in (mods["chan"] or {}).get("detail", ""):
        lines.append(f"  {mods['chan']['detail']}（{mods['chan']['bi_dir']}/{mods['chan']['zone']}）")
    od = ctx.get("overseas_drop") or 0.0
    if od <= -0.03:
        lines.append(f"■ 外围：SOX/纳指/标普 t-1 最差 {od:.1%}（外围大幅下杀）")
    lines.append(f"■ 盘中：创业板指 {ctx['intraday']:+.2f}%")
    dar = ctx.get("day_amount_ratio") or 0.0
    if dar:
        lines.append(f"■ 量能：今日累计量/昨量 {dar:.2f}（量价因子用昨日完整量）")
    lines.append(f"■ 数据：399006完整日线：{ctx.get('history_bars', len(ctx.get('closes') or []))}根，"
                 f"截至{ctx.get('history_last_date') or '-'}")
    if caps["triggers"]:
        lines.append("■ 硬风控：" + "；".join(caps["triggers"]))
        # 澄清主因：档位基准未越封顶线时，硬风控只是背景约束而非空仓/降档主因
        # （2026-08-27 实证：综合分 -0.41 已低于空仓线，回撤封顶 3 成未被触发执行，
        # 但并排展示易被误读为"空仓是风控逼的"）
        if tier_target <= float(caps.get("cap") or 1.0):
            lines.append(f"  （注：本次仓位由综合分 {res['score']:+.2f} 对档位线决定，"
                         f"硬风控仅限上限未实际生效）")
    else:
        lines.append("■ 硬风控：无触发")
    if ctx.get("snapshot_stale"):
        lines.append(f"⚠ 修正层数据源停更于 {ctx.get('snapshot_ts', '')}，"
                     f"贴水/资金/情绪基于旧快照，请结合盘中走势谨慎参考")
    if dec["note"] and not chg and "确认" not in dec["note"][0]:
        lines.append("■ " + "；".join(dec["note"]))
    # 策略失效熔断（P1-2，2026-08-29）：只告警不改仓位，样本不足时标注进度
    if health:
        if health.get("level") == "alert":
            lines.append("⚠ 策略健康度告警：" + "；".join(health["reasons"]))
            lines.append("  （仅提示，不自动改变仓位；请人工判断是否暂停跟投）")
        elif health.get("level") == "insufficient":
            lines.append(f"■ 策略健康度：{health['reasons'][0]}")
    lines.append("")
    lines.append("档位线：≥+0.40满仓｜≥-0.15九成｜≥-0.30战略六成底仓｜更低空仓")
    lines.append(f"升档需连续2日确认，降档当日生效；15:00 前下单有效。仅供参考。"
                 f"（代码 {_git_commit()}）")
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


def _configure_stdout() -> None:
    """让 Windows 非 UTF-8 控制台替换无法编码的报告字符，而不是崩溃。"""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        # pytest/caller 提供的类文件对象可能没有可重配置的底层编码。
        pass


def _snapshot_is_stale(snapshot: dict, today_s: str = None) -> bool:
    """判断增强快照是否可用于指定日期。

    快照有内容却缺失或带非法 ``ts`` 时，不能把未知时效当成新鲜；空快照
    另由报告层标记为缺源，因此不在这里重复标记为过期。
    """
    if not snapshot:
        return False
    raw_ts = snapshot.get("ts")
    if not isinstance(raw_ts, str) or not raw_ts.strip():
        return True
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw_ts.strip(), fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return True
    today_s = today_s or datetime.now(BJT).strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d") != today_s


def _completed_bar_count(df) -> int:
    """统计当前日期之前的完整日线根数，排除盘中、未来和非法日期。"""
    try:
        dates = pd.to_datetime(df.index, errors="coerce")
        today = pd.Timestamp(datetime.now(BJT).date())
        valid = ~pd.isna(dates)
        return int((valid & (dates.normalize() < today)).sum())
    except (AttributeError, TypeError, ValueError):
        return 0


def _is_trading_day(today=None) -> bool:
    """判断当前日期是否为中国交易日，节假日由公共日历识别。"""
    return _is_workday(today or datetime.now(BJT).date(), "cn")


# ---------------- 回测（核心层） ----------------

def _default_weights(val_w: float) -> dict:
    """v5.1 权重（2026-08-29 STR-04 合入，d日快照口径下重寻优）：趋势.35/量价.20/波动.20/估值0/落袋.25。
    val_w 参数保留仅为调用方签名兼容，不再注入估值权重（value_erp 打分关闭后
    唯一值{0.0}为死因子）。
    依据（v5.1 d日快照口径下 walk-forward OOS，fee=0）：趋势 ≤0.45 全区间优于 T.50；
    T.35/P.25 OOS +96.8%/夏普0.60/回撤-23.6% vs T.50 +83.8%/0.55/-25.4%——
    d 日快照已含当日涨跌，趋势因子时效性已够，提权趋势反而追当日涨跌；
    落袋（反向离场）在快照口径下更有价值。详见 docs/策略缺陷实验报告_20260828.md 第七章。"""
    return {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
            "估值": 0.00, "落袋": 0.25}


def backtest_metrics(df, fee: float = 0.0, pe_map: Optional[dict] = None,
                     val_span: int = 500, val_w: float = 0.10,
                     tiers: tuple = ct.TIERS, erp_cap: bool = False,
                     eval_start: Optional[int] = None,
                     eval_end: Optional[int] = None,
                     initial_prev: Optional[dict] = None) -> dict:
    """v4 核心层历史回测数值（无前视），供回测和寻优脚本共用。

    ``eval_start`` / ``eval_end`` 是半开区间 ``[eval_start, eval_end)``，
    表示只统计该窗口内的决策及次日收益；完整 ``df`` 仍用于计算滚动因子，
    因此窗口开始前必须保留至少 60 根 warmup 日线。``initial_prev`` 可传入
    窗口边界的 ``{"position": float, "pending": ...}`` 状态，用于 walk-forward
    跨折继承实际仓位。返回值除指标外还包含 ``navs``、``daily_rets``、
    ``bh_navs``、``final_state`` 等审计字段。时序为：信号日 d 只用 ≤d-1
    收盘，仓位吃 d+1 收益（对齐场外基金 T+1）；实时修正层和缠论不可回测。"""
    # 回测只接受完整历史日线；当前日期/未来日期的 bar 可能仍是盘中或异常数据，
    # 一律排除，避免把未收盘价格当成历史收盘价并污染最后一笔收益。
    frame = df.copy()
    frame.index = pd.to_datetime(frame.index)
    today = pd.Timestamp(datetime.now(BJT).date())
    frame = frame.loc[frame.index.normalize() < today]
    closes = frame["close"].tolist()
    amounts = (frame["amount"].tolist() if "amount" in frame else [0.0] * len(closes))
    dates = frame.index
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    n = len(closes)
    warmup = 60
    start = warmup if eval_start is None else int(eval_start)
    end = n - 1 if eval_end is None else int(eval_end)
    if n < warmup + 2:
        raise ValueError(f"回测至少需要 {warmup + 2} 根完整日线，实际 {n} 根")
    if start < warmup or end <= start or end >= n:
        raise ValueError(f"invalid evaluation window: {start}:{end} for {n} bars")
    # ERP 估值极端滤波（独立硬过滤，与打分脱钩）：
    # pe_map 存在时算便宜度分位序列，但 erp_cap 分支才启用（core 打分仍传 None，
    # 维持估值维关闭——经寻优 ERP 负贡献故不进打分，仅作"估值极贵封顶"）。
    erp_series = None
    erp = None  # 打分层不使用估值维（估值维负贡献，保持 None）；pe_map 缺失也安全
    if pe_map:
        pe = ipe.align_pe_by_dates(pe_map, date_strs)
        erp_series = ipe.pe_to_cheap_pctile(pe, val_span)
    signals = cf.core_signals(closes, amounts, erp_pctile=erp)
    comp = cf.dimension_score(signals, _default_weights(val_w))
    prev = dict(initial_prev or {"position": 0.0, "pending": None})
    nav, peak, navs = 1.0, 1.0, []
    switches = 0
    down_next10 = []
    daily_rets = []
    pos_sum = 0.0
    for d in range(start, end):
        caps = cf.defensive_state(closes[:d + 1], None,
                                   {"risk_off": False, "basis_min_ap": None,
                                    "intraday_pct": 0.0})
        cap = caps["cap"]
        if erp_cap and erp_series is not None and erp_series[d] is not None and \
                erp_series[d] < 0.10:
            # 便宜度<0.10=PE处于顶部10%（估值极贵）→封顶6成，与 score_all 同口径
            cap = min(cap, 0.6)
        # 口径（2026-08-28 v5.1 用户拍板）：决策用 d 日 14:45 快照（回测用 d 日收盘
        # 近似，14:45→15:00 价差接受），而非 d-1 收盘——核心因子反映"当天到现在的
        # 走势"，对当日加减仓更有意义。收益仍为 d 收盘→d+1 收盘（对齐场外基金 T+1）。
        dec = ct.decide_position(comp[d], cap, prev, tiers=tiers)
        fee_cost = 0.0
        if dec["changed"]:
            switches += 1
            fee_cost = fee * abs(dec["position"] - prev["position"])
            nav *= (1 - fee_cost)
            if dec["direction"] == "down" and d + 10 < end:
                down_next10.append(closes[d + 10] / closes[d] - 1.0)
        prev = {"position": dec["position"], "pending": dec["pending"]}
        pos_sum += dec["position"]
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        # 换仓费计入当日收益（此前只扣 nav 未计 daily_rets → 夏普不反映成本，
        # fee 敏感性分析失真；fee=0 时本行无影响）
        daily_rets.append((1 - fee_cost) * (1 + dec["position"] * r) - 1)
        peak = max(peak, nav)
        navs.append(nav)
    total = nav - 1.0
    years = len(navs) / 244.0
    cagr = nav ** (1 / years) - 1 if years > 0 else 0.0
    equity_curve = [1.0] + navs
    mdd = min(v / max(equity_curve[:i + 1]) - 1.0
              for i, v in enumerate(equity_curve)) if navs else 0.0
    mu = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
    sd = (sum((x - mu) ** 2 for x in daily_rets) / max(1, len(daily_rets) - 1)) ** 0.5
    sharpe = mu / sd * (244 ** 0.5) if sd > 0 else 0.0
    bh = closes[end] / closes[start] - 1.0
    bh_navs = [closes[i + 1] / closes[start] for i in range(start, end)]
    bh_equity_curve = [1.0] + bh_navs
    bh_mdd = min(v / max(bh_equity_curve[:i + 1]) - 1.0
                 for i, v in enumerate(bh_equity_curve)) if bh_navs else 0.0
    calmar = cagr / abs(mdd) if mdd else 0.0
    calmar_b = (closes[end] / closes[start]) ** (1 / max(1, years)) - 1
    calmar_b = calmar_b / abs(bh_mdd) if bh_mdd else 0.0
    dodge = (sum(1 for x in down_next10 if x < 0) / len(down_next10),
             sum(down_next10) / len(down_next10)) if down_next10 else (0.0, 0.0)
    return {"dates": dates, "start": start, "end": end, "total": total, "cagr": cagr,
            "sharpe": sharpe, "mdd": mdd, "calmar": calmar,
            "bh": bh, "bh_mdd": bh_mdd, "calmar_b": calmar_b,
            "switches": switches, "avg_pos": pos_sum / max(1, len(navs)),
            "n_navs": len(navs), "n_down": len(down_next10),
            "down_dodge": dodge, "has_val": erp_series is not None,
            "final_state": prev, "navs": navs, "bh_navs": bh_navs,
            "daily_rets": daily_rets}


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
        f"创业板仓位信号·v5核心层回测（{ds[s].date()} ~ {ds[-1].date()}，"
        f"{m['n_navs']}个交易日，成本{fee:.1%}/次）", "",
        f"策略累计 {m['total']:+.1%} / 年化 {m['cagr']:+.1%} / 夏普 {m['sharpe']:.2f} / "
        f"最大回撤 {m['mdd']:.1%} / 卡玛 {m['calmar']:.2f}",
        f"买入持有  {m['bh']:+.1%} / 最大回撤 {m['bh_mdd']:.1%} / 卡玛 {m['calmar_b']:.2f}",
        f"换仓 {m['switches']} 次｜平均仓位 {m['avg_pos']:.0%}",
        "",
        f"降档质量：{m['n_down']} 次减仓后10日市场 {dodge[1]:+.1%}（均值），"
        f"{dodge[0]:.0%} 段为下跌",
        "口径：信号日 d 使用 d 日 14:45 快照（回测用 d 日收盘近似），吃 d+1 收益（对齐场外基金T+1）；"
        "仅核心层9个注册因子五维（其中ERP关闭），实时修正层+缠论不可回测（影子期再评估）。" + val_note + "。",
    ])


# ---------------- 主流程 ----------------

def main():
    _configure_stdout()
    _load_local_env()
    ap = argparse.ArgumentParser(description="创业板仓位信号（CPO/PCB 场外基金手动执行版）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送不写状态")
    ap.add_argument("--push", action="store_true", help="推送 + 写状态（云端定时）")
    ap.add_argument("--backtest", action="store_true", help="核心层历史回测")
    ap.add_argument("--shadow", action="store_true", help="影子期因子IC报告")
    ap.add_argument("--force", action="store_true",
                    help="忽略同日去重，强制推送（手动复验用，如周六再验一次）")
    args = ap.parse_args()

    # 外部调度按工作日触发，但工作日 cron 仍可能落在中国法定节假日；
    # 正常推送必须跳过，避免把节假日写成信号日。--force 只供人工复验。
    if args.push and not args.force and not _is_trading_day():
        logger.info("今日非中国交易日，跳过创业板仓位信号推送")
        return

    # 历史源：新浪全量(12年，绕代理)→东财增量回退
    df = load_index_sina(SYMBOL)
    if df is None or df.empty:
        df = load_index_daily_full(SYMBOL, "20200101")
    if df is None or df.empty:
        raise SystemExit("创业板指日线获取失败，退出（不推送无数据信号）")
    complete_history = _completed_bar_count(df)
    if complete_history < MIN_SIGNAL_HISTORY:
        raise SystemExit(
            f"创业板指历史不足（实际 {complete_history} 根完整日线，至少需要 "
            f"{MIN_SIGNAL_HISTORY} 根完整日线），"
            "退出（不推送伪中性信号）"
        )
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
            print(f"  {f:<16} " + "  ".join(parts))
        # 修正层验门：raw 原始输入的分层单调性（1 日前瞻，离散档分 IC 分辨力弱，
        # 分层看"因子值高→收益高/低"是否单调，比单个 IC 更稳健）
        print("\n修正层原始输入 · 分层单调性（3组，1日前瞻）")
        for f in ("raw.basis_min_ap", "raw.main_net", "raw.down_pct", "raw.pcr"):
            lay = ct.layer_ic(hist, f)
            if not lay.get("ok"):
                print(f"  {f:<16} 样本不足（n={lay.get('n', 0)}，需≥{lay['min_samples'] if 'min_samples' in lay else 30}）")
                continue
            gs = "  ".join(f"组{g+1}:因子{gr[0]:+.2f}→收益{gr[1]:+.2%}(n={gr[2]})"
                           for g, gr in enumerate(lay["groups"]))
            tag = "单调✓" if lay["monotone"] else "非单调"
            print(f"  {f:<16} {gs} | 高低差{lay['spread']:+.2%} {tag}")
        return

    if args.backtest:
        # ERP 估值极端滤波（便宜度<0.1=PE顶部10% 封顶6成）：估值极贵时降仓。
        # 估值维不进打分（erp_cap 独立硬过滤，core 仍 erp_pctile=None）。
        print(run_backtest(df, pe_map=ipe.load_cy50_pe(PROJECT_ROOT),
                           erp_cap=True))
        return

    today = datetime.now(BJT).strftime("%Y-%m-%d")
    ctx = gather_context(df)
    res = score_all(ctx)

    state = load_state()
    if args.push and not args.force and str(state.get("last_date") or "") == today:
        logger.warning("今日已推送过（last_date=%s），去重跳过（--force 可略过）", today)
        return
    prev_pos = float(state.get("position") or 0.0)
    prev = {"position": prev_pos, "pending": state.get("pending")}
    dec = ct.decide_position(res["score"], res["caps"]["cap"], prev)

    # 策略失效熔断（P1-2）：基于影子 history 的累计净值评估健康度，仅告警不改仓位
    _health = strategy_health(state.get("history") or [])
    report = render_report(today, res, ctx, dec, prev_pos, health=_health)
    print("\n" + report + "\n")

    if args.push:
        if push_report(report, f"创业板仓位信号 {today[5:]}"):
            state.update({"last_date": today, "position": dec["position"],
                          "pending": dec["pending"], "last_score": res["score"]})
            update_shadow_history(state, ctx, today, res["score"], res,
                                  dec["position"], prev_pos)
            if save_state(state) is False:
                raise RuntimeError("状态写入失败，无法确认本次信号已持久化")
            logger.info("已推送并写状态（仓位 %.0f）", dec["position"])
        else:
            logger.warning("推送失败，状态不更新（明日重跑）")
    elif args.dry_run:
        print("（dry-run：不推送不写状态）")


if __name__ == "__main__":
    main()
