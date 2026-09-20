# -*- coding: utf-8 -*-
"""
创业板择时质量周报（chinext_timing_backtest.py）
====================================================
定位：对 run_chinext_timing.py（v5.1）每日推送的仓位决策做质量评估。
这是真正可回测的决策信号（仓位 0/60/90/100%），而非事件方向标签。

数据流（只读，不写任何状态）：
- 读取 Gist chinext_timing_state.json 的 history 数组（本地 logs/ 降级）
- history 每条字段：date, score, core, basis, flow, mood, news, chan, stock,
  kospi, sox, vix, a50, position, prev_pos, raw, cap, cap_triggers,
  next_ret, nav, bh_nav, r3, r5, r10, index_snapshot, sig, probe,
  fwd3_off/fwd5_off/fwd10_off
- r3/r5/r10 为日后 3/5/10 个交易日实际收益，尾部 null 属正常（待回填）

报告章节：
1. 推送信号对账（逐日信号 vs 实际涨跌）
2. 仓位决策审计（核心章节）
3. 踏空/躲跌归因
4. score→次日收益 IC（Spearman）
5. score 分层单调性（按 v5.1 档位边界）
6. 七层因子 IC
7. 结论提示（事实陈述，禁止收益承诺）

用法：
  python scripts/chinext_timing_backtest.py                 # 打印 + 落盘
  python scripts/chinext_timing_backtest.py --push          # 推送摘要
  python scripts/chinext_timing_backtest.py --days 30       # 限制窗口
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".ENV")

from src.tools.push import push_via_wecom, push_via_pushplus
from src.strategy.state_io import get_gist_config, read_gist_json

logger = logging.getLogger("chinext_timing_backtest")

TIMING_STATE_FILENAME = "chinext_timing_state.json"
_LOCAL_STATE_PATH = PROJECT_ROOT / "logs" / TIMING_STATE_FILENAME
REPORT_PATH = PROJECT_ROOT / "logs" / "chinext_timing_quality_report.md"

BJT = timezone(timedelta(hours=8))
HORIZONS = (1, 3, 5, 10)
MIN_IC_DAYS = 20  # IC 评估最小样本（配对交易日数）
FLAT_RET_THRESHOLD = 0.003  # 次日绝对收益小于 0.3% 视为基本持平
MIN_SIGNAL_SAMPLE = 20  # 信号偏差结论的最小次日回填样本

# v5.2 生产档位（与 src/strategy/chinext_timing.py 的 TIERS 一致，2026-09-20 切换）
# TIERS = ((0.30, 1.0), (-0.25, 0.9), (-0.30, 0.6))
# score >= 0.30 → 100%; score >= -0.25 → 90%; score >= -0.30 → 60%; else 0%
TIERS = ((0.30, 1.0), (-0.25, 0.9), (-0.30, 0.6))

# 七层因子（与 history 字段对应）
SEVEN_FACTORS = ("core", "basis", "flow", "mood", "news", "chan", "stock")
# 数据契约：history 中 next_ret/r3/r5/r10 为小数（0.0121=+1.21%），展示层统一 ×100


# ============================================================
# 数据源
# ============================================================
def _load_timing_state() -> dict:
    """读取择时状态（Gist 优先，本地降级）；失败返回 {}"""
    gist_token, gist_id = get_gist_config()
    if gist_token and gist_id:
        return read_gist_json(TIMING_STATE_FILENAME, gist_token, gist_id,
                              user_agent="chinext-timing-backtest")
    try:
        state = json.loads(_LOCAL_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


# ============================================================
# 统计工具
# ============================================================
def _rank(values: list) -> list:
    """平均秩（并列值取平均秩），Spearman 用"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list, ys: list) -> float:
    """Spearman 秩相关（无 scipy 依赖）；样本<3 或任一侧零方差返回 0"""
    n = len(xs)
    if n < 3:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = sum((r - mx) ** 2 for r in rx)
    vy = sum((r - my) ** 2 for r in ry)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / (vx ** 0.5 * vy ** 0.5)


def _safe_mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _max_drawdown(nav_series: list) -> float:
    """从净值序列计算最大回撤（%）"""
    if not nav_series or len(nav_series) < 2:
        return 0.0
    peak = nav_series[0]
    mdd = 0.0
    for v in nav_series:
        if v > peak:
            peak = v
        dd = (v / peak - 1) * 100 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return round(mdd, 2)


# ============================================================
# 分析核心
# ============================================================
def score_to_tier(score: float) -> float:
    """按 v5.1 档位边界将 score 映射到仓位"""
    for th, pos in TIERS:
        if score >= th:
            return pos
    return 0.0


def classify_signal(position: float, next_ret: float,
                    flat_threshold: float = FLAT_RET_THRESHOLD) -> str:
    """按推送仓位和次日实际收益给出单日对账判定。"""
    if next_ret is None:
        return "待回填"
    if abs(next_ret) < flat_threshold:
        return "基本持平"
    if position > 0:
        return "符合" if next_ret > 0 else "偏差"
    return "躲过" if next_ret < 0 else "错过"


def _signal_note(position: float, next_ret: float, verdict: str,
                 flat_threshold: float) -> str:
    """生成逐日对账表的备注，不参与判定或统计。"""
    signal = "持仓信号" if position > 0 else "空仓信号"
    if verdict == "待回填":
        return "次日实际未回填"
    if verdict == "基本持平":
        return f"{signal}，|次日实际|<{flat_threshold * 100:.2f}%"
    direction = "上涨" if next_ret > 0 else "下跌"
    return f"{signal}，次日{direction}"


def compute_signal_reconciliation(
    history: list, flat_threshold: float = FLAT_RET_THRESHOLD
) -> list:
    """构造逐条推送信号对账结果；不修改传入 history。"""
    rows = []
    for item in history:
        position = item.get("position", 0.0)
        next_ret = item.get("next_ret")
        verdict = classify_signal(position, next_ret, flat_threshold)
        rows.append({
            "date": item.get("date", "—"),
            "position": position,
            "score": item.get("score"),
            "next_ret": next_ret,
            "r3": item.get("r3"),
            "r5": item.get("r5"),
            "r10": item.get("r10"),
            "signal": "持仓信号" if position > 0 else "空仓信号",
            "verdict": verdict,
            "note": _signal_note(position, next_ret, verdict, flat_threshold),
        })
    return rows


def compute_signal_bias_stats(
    history: list, flat_threshold: float = FLAT_RET_THRESHOLD
) -> dict:
    """统计空仓/持仓信号与次日实际走势的偏差。"""
    rows = compute_signal_reconciliation(history, flat_threshold)
    empty = {
        "total": 0,
        "n": 0,
        "pending": 0,
        "avoided": 0,
        "missed": 0,
        "flat": 0,
        "avoided_total": 0.0,
        "missed_total": 0.0,
    }
    holding = {
        "total": 0,
        "n": 0,
        "pending": 0,
        "conforming": 0,
        "deviation": 0,
        "flat": 0,
        "conforming_total": 0.0,
        "deviation_total": 0.0,
    }

    for row in rows:
        stats = holding if row["position"] > 0 else empty
        stats["total"] += 1
        next_ret = row["next_ret"]
        if next_ret is None:
            stats["pending"] += 1
            continue
        stats["n"] += 1
        if row["verdict"] == "基本持平":
            stats["flat"] += 1
        elif row["verdict"] == "躲过":
            stats["avoided"] += 1
            stats["avoided_total"] += -next_ret
        elif row["verdict"] == "错过":
            stats["missed"] += 1
            stats["missed_total"] += next_ret
        elif row["verdict"] == "符合":
            stats["conforming"] += 1
            stats["conforming_total"] += next_ret
        elif row["verdict"] == "偏差":
            stats["deviation"] += 1
            stats["deviation_total"] += -next_ret

    for stats in (empty, holding):
        for key in ("avoided_total", "missed_total", "conforming_total", "deviation_total"):
            if key in stats:
                stats[key] = round(stats[key], 6)

    filled_n = sum(1 for row in rows if row["next_ret"] is not None)
    pending_n = len(rows) - filled_n
    missed_total = empty["missed_total"]
    deviation_total = holding["deviation_total"]
    if missed_total > deviation_total:
        bias = "信号偏空"
    elif deviation_total > missed_total:
        bias = "信号偏多"
    else:
        bias = "无显著偏差"
    conclusion = bias
    if filled_n < MIN_SIGNAL_SAMPLE:
        conclusion = f"{bias}；样本不足，暂不下结论"

    return {
        "n": filled_n,
        "pending": pending_n,
        "total": len(rows),
        "empty": empty,
        "holding": holding,
        "bias": bias,
        "conclusion": conclusion,
        "flat_threshold": flat_threshold,
    }


def _fmt_return(value: float, missing: str = "—") -> str:
    return f"{value * 100:+.2f}%" if value is not None else missing


def render_signal_reconciliation_table(
    history: list, flat_threshold: float = FLAT_RET_THRESHOLD
) -> str:
    """渲染逐日对账表，按日期倒序排列。"""
    rows = compute_signal_reconciliation(history, flat_threshold)
    rows.sort(key=lambda row: row["date"], reverse=True)
    lines = [
        "| 日期 | 推送仓位 | score | 次日实际 | 3日 | 5日 | 10日 | 判定 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        position = f"{row['position'] * 100:.0f}%"
        score = f"{row['score']:+.3f}" if row["score"] is not None else "—"
        lines.append(
            f"| {row['date']} | {position} | {score} | "
            f"{_fmt_return(row['next_ret'], '待回填')} | "
            f"{_fmt_return(row['r3'])} | {_fmt_return(row['r5'])} | "
            f"{_fmt_return(row['r10'])} | {row['verdict']} | {row['note']} |"
        )
    return "\n".join(lines)


def compute_current_cycle(history: list) -> dict:
    """返回最近一次仓位变动及其后的市场累计涨跌。"""
    ordered = sorted(history, key=lambda item: item.get("date", ""))
    if not ordered:
        return {
            "change_date": None,
            "previous_position": None,
            "position": None,
            "cumulative_ret": None,
            "filled_n": 0,
        }

    change_index = 0
    for i in range(1, len(ordered)):
        if ordered[i].get("position", 0.0) != ordered[i - 1].get("position", 0.0):
            change_index = i

    change = ordered[change_index]
    cumulative = 1.0
    filled_n = 0
    for item in ordered[change_index:]:
        next_ret = item.get("next_ret")
        if next_ret is not None:
            cumulative *= 1 + next_ret
            filled_n += 1
    return {
        "change_date": change.get("date"),
        "previous_position": (ordered[change_index - 1].get("position", 0.0)
                               if change_index > 0 else 0.0),
        "position": change.get("position", 0.0),
        "cumulative_ret": round(cumulative - 1, 8) if filled_n else None,
        "filled_n": filled_n,
    }


def compute_ic(history: list) -> dict:
    """score→次日收益 Spearman IC"""
    pairs = [(h["score"], h["next_ret"]) for h in history
             if h.get("next_ret") is not None and h.get("score") is not None]
    n = len(pairs)
    if n < 3:
        return {"n": n, "ic": None}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return {"n": n, "ic": round(_spearman(xs, ys), 4)}


def compute_stratification(history: list) -> dict:
    """score 分层单调性：按 v5.1 档位分档，输出各档 mean(next_ret)"""
    buckets = {}
    for h in history:
        if h.get("next_ret") is None:
            continue
        tier = score_to_tier(h.get("score", 0))
        buckets.setdefault(tier, []).append(h["next_ret"])
    if not buckets:
        return {"n": 0, "layers": {}}
    layers = {}
    for tier in sorted(buckets.keys()):
        vals = buckets[tier]
        layers[tier] = {
            "n": len(vals),
            "mean_ret": round(sum(vals) / len(vals), 4)
        }
    # 单调方向：按档位从高到低，高档收益≥低档收益为"高仓位→高收益"（mono_up）
    # mono_up: 从高档→低档，mean_ret 单调不增（高档收益 ≥ 低档收益）
    # mono_down: 从高档→低档，mean_ret 单调不减（高档收益 ≤ 低档收益）
    sorted_tiers = sorted(layers.keys(), reverse=True)
    mean_ret_ordered = [layers[t]["mean_ret"] for t in sorted_tiers]
    mono_up = all(mean_ret_ordered[i] >= mean_ret_ordered[i + 1]
                  for i in range(len(mean_ret_ordered) - 1))
    mono_down = all(mean_ret_ordered[i] <= mean_ret_ordered[i + 1]
                    for i in range(len(mean_ret_ordered) - 1))
    direction = "单调递增（高仓位→高收益）" if mono_up else \
                "单调递减" if mono_down else "非单调"
    return {"n": sum(len(v) for v in buckets.values()), "layers": layers,
            "direction": direction}


def compute_decision_audit(history: list) -> list:
    """仓位决策审计：遍历 history 找 position 变动点"""
    audits = []
    for i, h in enumerate(history):
        if i == 0 or h["position"] != history[i - 1].get("position", h["position"]):
            # 变动点（含首日）
            prev_pos = history[i - 1].get("position", 0.0) if i > 0 else 0.0
            new_pos = h["position"]
            r3 = h.get("r3")
            r5 = h.get("r5")
            r10 = h.get("r10")

            # 判定
            filled = [x for x in [r3, r5, r10] if x is not None]
            if not filled:
                verdict = "待验证（收益未回填）"
            elif new_pos < prev_pos:
                # 减仓
                avg_ret = sum(filled) / len(filled)
                verdict = "正确避险" if avg_ret < 0 else "踏空（减仓后上涨）"
            elif new_pos > prev_pos:
                # 加仓
                avg_ret = sum(filled) / len(filled)
                verdict = "正确加仓" if avg_ret > 0 else "加仓后下跌"
            else:
                verdict = "维持"

            audits.append({
                "date": h["date"],
                "prev_pos": prev_pos,
                "new_pos": new_pos,
                "r3": r3,
                "r5": r5,
                "r10": r10,
                "verdict": verdict,
            })
    return audits


def compute_miss_avoid(history: list) -> dict:
    """踏空/躲跌归因"""
    # 空仓避险贡献：逐日 (1-position) × (-next_ret) 累计
    # 减仓后跌 → 躲跌为正贡献；减仓后涨 → 踏空为负贡献
    daily_contrib = []
    for h in history:
        pos = h.get("position", 0.0)
        nr = h.get("next_ret")
        if nr is not None:
            contrib = (1 - pos) * (-nr)
            daily_contrib.append(contrib)

    total_contrib = round(sum(daily_contrib), 4) if daily_contrib else 0.0

    # nav vs bh_nav
    nav_series = [h["nav"] for h in history if h.get("nav") is not None]
    bh_series = [h["bh_nav"] for h in history if h.get("bh_nav") is not None]

    nav_final = nav_series[-1] if nav_series else None
    bh_final = bh_series[-1] if bh_series else None
    diff_pp = round((nav_final - bh_final) * 100, 2) if (nav_final is not None and bh_final is not None) else None

    nav_mdd = _max_drawdown(nav_series) if nav_series else None
    bh_mdd = _max_drawdown(bh_series) if bh_series else None

    return {
        "total_contrib": total_contrib,
        "nav_final": nav_final,
        "bh_final": bh_final,
        "diff_pp": diff_pp,
        "nav_mdd": nav_mdd,
        "bh_mdd": bh_mdd,
    }


def compute_factor_ics(history: list) -> dict:
    """七层因子 IC：各因子 Spearman 对 next_ret"""
    result = {}
    for factor in SEVEN_FACTORS:
        pairs = [(h[factor], h["next_ret"]) for h in history
                 if h.get("next_ret") is not None and h.get(factor) is not None]
        n = len(pairs)
        if n < 3:
            result[factor] = {"n": n, "ic": None}
        elif n < MIN_IC_DAYS:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            result[factor] = {"n": n, "ic": round(_spearman(xs, ys), 4),
                              "note": "样本不足"}
        else:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            result[factor] = {"n": n, "ic": round(_spearman(xs, ys), 4)}
    return result


# ============================================================
# 报告
# ============================================================
def build_report(history: list, days: int = None) -> str:
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")
    n_total = len(history)
    dates = [h["date"] for h in history if h.get("date")]
    earliest = min(dates) if dates else "—"
    latest = max(dates) if dates else "—"

    # 各 horizon 已回填条数
    filled = {}
    for h_label, key in [("1日", "next_ret"), ("3日", "r3"), ("5日", "r5"), ("10日", "r10")]:
        filled[h_label] = sum(1 for h in history if h.get(key) is not None)

    lines = ["# 创业板择时质量周报",
             f"生成时间: {now}",
             f"**覆盖与样本**: history 共 {n_total} 条，{earliest} ~ {latest}",
             f"**各 horizon 已回填**: " + " / ".join(f"{k} {v}条" for k, v in filled.items()),
             ""]

    # 1. 推送信号对账
    signal_stats = compute_signal_bias_stats(history)
    current_cycle = compute_current_cycle(history)
    empty_stats = signal_stats["empty"]
    holding_stats = signal_stats["holding"]
    lines.append("## 1. 推送信号对账")
    lines.append("")
    lines.append("### 1.1 逐日对账")
    lines.append("")
    lines.append(render_signal_reconciliation_table(history))
    lines.append("")

    lines.append("### 1.2 判定规则")
    lines.append("")
    lines.append("- 仓位 > 0 为“持仓信号”，仓位 = 0 为“空仓信号”。")
    lines.append("- 空仓 + 次日下跌 = 躲过（一致）；空仓 + 次日上涨 = 错过（偏差）。")
    lines.append("- 持仓 + 次日上涨 = 符合（一致）；持仓 + 次日下跌 = 偏差。")
    lines.append(f"- |次日实际| < {FLAT_RET_THRESHOLD * 100:.2f}% 为“基本持平”，不计入偏差统计。")
    lines.append("- 次日实际未回填 = 待回填。")
    lines.append("")

    lines.append("### 1.3 偏差统计")
    lines.append("")
    lines.append(
        f"- 对账样本：次日已回填 {signal_stats['n']} 条，"
        f"待回填 {signal_stats['pending']} 条。"
    )
    lines.append(
        f"- 空仓信号日：已回填 {empty_stats['n']} 条中，"
        f"躲过 {empty_stats['avoided']} / 错过 {empty_stats['missed']} / "
        f"持平 {empty_stats['flat']}。"
    )
    lines.append(
        f"  - 错过合计（涨幅）：{_fmt_return(empty_stats['missed_total'])}；"
        f"躲过合计（跌幅）：{_fmt_return(empty_stats['avoided_total'])}"
    )
    lines.append(
        f"- 持仓信号日：已回填 {holding_stats['n']} 条中，"
        f"符合 {holding_stats['conforming']} / 偏差 {holding_stats['deviation']} / "
        f"持平 {holding_stats['flat']}。"
    )
    lines.append(
        f"  - 符合合计（涨幅）：{_fmt_return(holding_stats['conforming_total'])}；"
        f"偏差合计（跌幅）：{_fmt_return(holding_stats['deviation_total'])}"
    )
    lines.append("- 偏空/偏多按错过涨幅合计与持仓偏差跌幅合计较大者判断。")
    lines.append(f"- 倾向结论：{signal_stats['conclusion']}")
    lines.append("")

    lines.append("### 1.4 当前周期")
    lines.append("")
    if current_cycle["change_date"] is None:
        lines.append("- 无仓位变动记录。")
    else:
        lines.append(
            f"- 最近一次仓位变动：{current_cycle['change_date']}，"
            f"{current_cycle['previous_position'] * 100:.0f}%→"
            f"{current_cycle['position'] * 100:.0f}%。"
        )
        if current_cycle["cumulative_ret"] is None:
            lines.append("- 此后市场累计涨跌：待回填。")
        else:
            lines.append(
                f"- 此后市场累计涨跌：{_fmt_return(current_cycle['cumulative_ret'])} "
                f"（按该日信号后的次日收益，已回填 {current_cycle['filled_n']} 条）。"
            )
    lines.append("")

    # 2. 仓位决策审计
    audits = compute_decision_audit(history)
    lines.append("## 2. 仓位决策审计")
    lines.append("")
    if not audits:
        lines.append("- 无仓位变动记录")
    else:
        lines.append(f"共 {len(audits)} 笔决策（含首日）")
        lines.append("")
        lines.append("| 日期 | 旧仓位→新仓位 | r3 | r5 | r10 | 判定 |")
        lines.append("|---|---|---|---|---|---|")
        for audit in audits:
            lines.append(
                f"| {audit['date']} | {audit['prev_pos'] * 100:.0f}%→"
                f"{audit['new_pos'] * 100:.0f}% | {_fmt_return(audit['r3'])} | "
                f"{_fmt_return(audit['r5'])} | {_fmt_return(audit['r10'])} | "
                f"{audit['verdict']} |"
            )
    lines.append("")

    # 3. 踏空/躲跌归因
    miss = compute_miss_avoid(history)
    lines.append("## 3. 踏空/躲跌归因")
    lines.append("")
    lines.append(f"- 空仓避险累计贡献: {miss['total_contrib']*100:+.4f}%")
    if miss['nav_final'] is not None:
        lines.append(f"- 策略净值 (nav): {miss['nav_final']:.4f} | 买入持有 (bh_nav): {miss['bh_final']:.4f}")
        lines.append(f"- 差值: {miss['diff_pp']:+.2f} pp")
        lines.append(f"- 策略最大回撤: {miss['nav_mdd']:.2f}% | 买入持有最大回撤: {miss['bh_mdd']:.2f}%")
    lines.append("")

    # 4. IC
    ic_result = compute_ic(history)
    lines.append("## 4. score→次日收益 IC")
    lines.append("")
    if ic_result["n"] < MIN_IC_DAYS:
        lines.append(f"> ⚠️ 样本不足（n={ic_result['n']} < {MIN_IC_DAYS}），仅供参考")
    if ic_result["ic"] is not None:
        lines.append(f"- Spearman IC: {ic_result['ic']:+.4f}（n={ic_result['n']}）")
    else:
        lines.append("- IC: 无法计算（配对样本 <3）")
    lines.append("")

    # 5. 分层单调性
    strat = compute_stratification(history)
    lines.append("## 5. score 分层单调性")
    lines.append("")
    lines.append("档位口径（v5.2 TIERS）：score≥0.30→100% / ≥-0.25→90% / ≥-0.30→60% / 其他→0%")
    lines.append("")
    if strat["n"] == 0:
        lines.append("- 无有效分层样本")
    else:
        lines.append("| 档位 | n | mean(next_ret%) |")
        lines.append("|---|---|---|")
        for tier, info in sorted(strat["layers"].items(), reverse=True):
            lines.append(f"| {tier*100:.0f}% | {info['n']} | {info['mean_ret']*100:+.4f} |")
        lines.append(f"- 单调方向: {strat['direction']}")
    lines.append("")

    # 6. 七层因子 IC
    factor_ics = compute_factor_ics(history)
    lines.append("## 6. 七层因子 IC（Spearman，对 next_ret）")
    lines.append("")
    lines.append("因子: core/basis/flow/mood/news/chan/stock（chan/海外样本少可跳过，注明）")
    lines.append("")
    lines.append("| 因子 | n | IC | 备注 |")
    lines.append("|---|---|---|---|")
    factor_labels = {
        "core": "核心层", "basis": "贴水", "flow": "资金", "mood": "情绪",
        "news": "资讯", "chan": "缠论", "stock": "旭创双确认"
    }
    for f in SEVEN_FACTORS:
        info = factor_ics[f]
        ic_str = f"{info['ic']:+.4f}" if info['ic'] is not None else "—"
        note = info.get("note", "")
        if f in ("chan", "stock") and info['n'] < MIN_IC_DAYS:
            note = "样本不足（海外/个股数据稀疏）" if note == "样本不足" else note
        lines.append(f"| {factor_labels.get(f, f)} ({f}) | {info['n']} | {ic_str} | {note} |")
    lines.append("")

    # 7. 结论提示
    lines.append("## 7. 结论提示")
    lines.append("")
    n_changes = len([a for a in audits if a['date'] != earliest])
    lines.append(f"- 样本 {n_total} 天，仓位变动 {n_changes} 次（不含首日），暂无统计显著性。")
    lines.append("- 样本量 <20 时 IC/分层结论仅供参考，需持续积累后复核。")
    lines.append("- 本报告仅做事实陈述，不构成任何收益承诺或投资建议。")
    lines.append("")

    return "\n".join(lines)


def do_push(title: str, content: str) -> dict:
    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    if pushplus_token:
        return push_via_pushplus(pushplus_token, title, content)
    if wecom_webhook:
        return push_via_wecom(wecom_webhook, title, content)
    return {"code": 400, "msg": "未配置推送后端"}


def main():
    parser = argparse.ArgumentParser(description="创业板择时质量周报")
    parser.add_argument("--days", type=int, default=None,
                        help="限制窗口天数（默认全部 history）")
    parser.add_argument("--push", action="store_true", help="推送报告摘要到微信")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    state = _load_timing_state()
    history = state.get("history", [])
    if not history:
        print("无 history 数据可评估（chinext_timing_state.json 缺 history）")
        return

    # 按 days 截取
    if args.days and args.days > 0:
        cutoff = (datetime.now(BJT) - timedelta(days=args.days)).strftime("%Y-%m-%d")
        history = [h for h in history if h.get("date", "") >= cutoff]

    if not history:
        print(f"截取窗口 {args.days} 天后无数据")
        return

    report = build_report(history, days=args.days)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(report)
    print(f"\n[报告已保存] {REPORT_PATH}")

    if args.push:
        # 推送摘要以逐条信号对账为主
        signal_stats = compute_signal_bias_stats(history)
        empty_stats = signal_stats["empty"]
        holding_stats = signal_stats["holding"]
        dates = [h["date"] for h in history]
        summary_lines = [
            "创业板择时质量周报",
            f"覆盖: {min(dates)}~{max(dates)} 共{len(history)}天",
            f"次日已回填: {signal_stats['n']}/{signal_stats['total']}条",
            f"空仓信号: 躲过{empty_stats['avoided']} / 错过{empty_stats['missed']} / 持平{empty_stats['flat']}",
            f"持仓信号: 符合{holding_stats['conforming']} / 偏差{holding_stats['deviation']} / 持平{holding_stats['flat']}",
            f"倾向: {signal_stats['conclusion']}",
        ]
        summary = "\n".join(summary_lines)
        r = do_push("创业板择时质量周报", summary)
        print(f"[推送] code={r.get('code', r.get('errcode'))}")


if __name__ == "__main__":
    main()
