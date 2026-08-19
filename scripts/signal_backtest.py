# -*- coding: utf-8 -*-
"""
信号有效性回测（signal_backtest.py）—— P2-3（2026-08-19）
====================================================
定位：追踪工具的"可信度基石"。用 pushed_events 历史 + 行情数据统计
"推送方向 vs 后 1/3/5 日行情一致率"，按范围/方向/板块分组，
产出信号质量报告，直接指导推送阈值与 LLM prompt 调优。

数据流（只读，不写任何状态）：
- 已推事件: real_time_push 的 real_time_state.json（云端 Gist 优先，本地降级）
  pushed_events 条目: {stocks, entities, events, numbers, sectors, scope, title_norm, dir, t}
- 行情: 新浪日K（与 factor_collector 同源口径）
- 个股代码解析: 腾讯 smartbox 搜索（名称→代码）

回测口径：
- 标的映射: stocks 非空 → 第一个股票名（smartbox 解析代码）；
  否则 scope=market → 上证指数；sector/无 stocks → 跳过（v1 不做板块指数映射）
- 基准价: 事件当日收盘价（当日非交易日则其后第一个交易日）
- 一致判定: 利多组(bullish/mildly_bullish) 后N日收益>0 = 一致；
  利空组(bearish/mildly_bearish) 后N日收益<0 = 一致
- 样本门槛: 分组样本 <10 只列计数不算比率（防小样本噪声误导）

用法：
  python scripts/signal_backtest.py                 # 打印报告 + 存 logs/signal_quality_report.md
  python scripts/signal_backtest.py --days 30      # 只回测最近 30 天
  python scripts/signal_backtest.py --push         # 报告摘要推送微信
"""
import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".ENV")

from src.tools.push import push_via_wecom, push_via_pushplus

logger = logging.getLogger("signal_backtest")

REALTIME_STATE_FILENAME = "real_time_state.json"
_REALTIME_STATE_PATH = PROJECT_ROOT / "logs" / "real_time_state.json"
REPORT_PATH = PROJECT_ROOT / "logs" / "signal_quality_report.md"

HORIZONS = (1, 3, 5)          # 后 N 个交易日
MIN_GROUP_SAMPLE = 10          # 分组比率门槛
DEFAULT_DAYS = 60              # 默认回测窗口
MARKET_INDEX = {"name": "上证指数", "symbol": "sh000001"}

# P4-6（2026-08-19）：方向维度 IC 加权
MIN_IC_DAYS = 20               # IC 评估最小样本（配对交易日数）
IC_FLOOR = 0.1                 # 权重收缩地板：max(IC,0)+floor，防早期样本噪声过度集中

_DIR_GROUP = {"bullish": "利多", "mildly_bullish": "利多",
              "bearish": "利空", "mildly_bearish": "利空"}

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ============================================================
# 数据源
# ============================================================
def _http_get(url: str, params: dict = None, encoding: str = None) -> str:
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=10)
        if encoding:
            r.encoding = encoding
        return r.text or ""
    except Exception as e:
        logger.warning(f"请求失败 {url}: {e}")
        return ""


def _load_realtime_state() -> dict:
    """已推事件状态：云端 Gist 优先，本地降级；失败返回 {}"""
    state = {}
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            headers = {"Authorization": f"token {gist_token}",
                       "Accept": "application/vnd.github+json",
                       "User-Agent": "stock-news-agent-backtest"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            fobj = (resp.json().get("files") or {}).get(REALTIME_STATE_FILENAME)
            if fobj is not None:
                state = json.loads(fobj.get("content") or "{}")
        except Exception as e:
            logger.warning(f"Gist 状态读取失败，降级本地: {e}")
    if not state:
        try:
            state = json.loads(_REALTIME_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return state


def _fetch_kline(symbol: str, lmt: int = 120) -> list:
    """新浪日K → [{date, close}]（升序；与 factor_collector 同源口径）"""
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(lmt)}
    text = _http_get(
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params=params,
        headers={"Referer": "http://finance.sina.com.cn", **_HEADERS},
    )
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            out.append({"date": str(item["day"])[:10], "close": float(item["close"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _resolve_symbol(name: str, cache: dict) -> str:
    """股票名 → 带前缀 symbol（腾讯 smartbox: "sz~300308~中际旭创~..."）；失败返回 """""
    name = str(name).strip()
    if not name:
        return ""
    if name in cache:
        return cache[name]
    text = _http_get("http://smartbox.gtimg.cn/s3/",
                     params={"v": "2", "q": name, "t": "gp"}, encoding="gbk")
    symbol = ""
    for line in text.split(";"):
        if "~" not in line:
            continue
        payload = line.partition("=")[2].strip().strip('"')
        parts = payload.split("~")
        # 格式: 市场~代码~名称~拼音~类型；名称须精确匹配（防"华创"命中"华创证券"外的模糊项）
        if len(parts) >= 3 and parts[2] == name and parts[0] in ("sh", "sz", "bj"):
            symbol = f"{parts[0]}{parts[1]}"
            break
    cache[name] = symbol
    return symbol


# ============================================================
# 回测核心
# ============================================================
def _forward_returns(klines: list, event_date: str) -> dict:
    """事件日基准收盘价 + 后 N 日收益（%）。

    事件日非交易日（周末/停牌）→ 用其后第一个交易日收盘为基准。
    返回 {"base": close, "ret_1": %, "ret_3": %, "ret_5": %}，不足 N 日者缺键。
    """
    dates = [k["date"] for k in klines]
    idx = None
    for i, d in enumerate(dates):
        if d >= event_date:  # 首个 >= 事件日的交易日
            idx = i
            break
    if idx is None or idx >= len(klines) - 1:
        return {}
    base = klines[idx]["close"]
    out = {"base": base, "entry_date": dates[idx]}
    for n in HORIZONS:
        j = idx + n
        if j < len(klines) and base:
            out[f"ret_{n}"] = round((klines[j]["close"] / base - 1) * 100, 2)
    return out


def backtest(events: list, days: int = DEFAULT_DAYS) -> dict:
    """对已推事件做方向一致率回测 → 聚合结果 dict"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    in_window = [e for e in events if str(e.get("t", ""))[:10] >= since]
    symbol_cache = {}
    kline_cache = {}

    results = []      # 每条评估明细
    skipped = defaultdict(int)
    for e in in_window:
        dir_raw = str(e.get("dir", "") or "")
        group = _DIR_GROUP.get(dir_raw)
        title = str(e.get("title_norm", "") or "")
        t = str(e.get("t", ""))
        if not group:
            skipped["未标注方向"] += 1
            continue
        stocks = e.get("stocks") or []
        if stocks:
            target_name = str(stocks[0])
            symbol = _resolve_symbol(target_name, symbol_cache)
            if not symbol:
                skipped["代码解析失败"] += 1
                continue
        elif str(e.get("scope", "")) == "market":
            target_name, symbol = MARKET_INDEX["name"], MARKET_INDEX["symbol"]
        else:
            skipped["无个股标的(板块级)"] += 1
            continue
        if symbol not in kline_cache:
            kline_cache[symbol] = _fetch_kline(symbol)
        klines = kline_cache[symbol]
        if not klines:
            skipped["行情缺失"] += 1
            continue
        fr = _forward_returns(klines, t[:10])
        if not fr:
            skipped["无后市数据"] += 1
            continue
        rec = {
            "t": t, "title": title[:40], "target": target_name, "symbol": symbol,
            "group": group, "dir": dir_raw, "sectors": [str(s) for s in (e.get("sectors") or [])][:3],
            **{k: fr.get(k) for k in ("entry_date", "ret_1", "ret_3", "ret_5")},
        }
        for n in HORIZONS:
            ret = fr.get(f"ret_{n}")
            if isinstance(ret, (int, float)):
                rec[f"hit_{n}"] = (ret > 0) if group == "利多" else (ret < 0)
        results.append(rec)

    # ---- 聚合 ----
    def _agg(rows: list) -> dict:
        out = {"n": len(rows)}
        for n in HORIZONS:
            hits = [r for r in rows if isinstance(r.get(f"hit_{n}"), bool)]
            out[f"n_{n}"] = len(hits)
            out[f"hit_{n}"] = sum(1 for r in hits if r[f"hit_{n}"]) if hits else None
        return out

    summary = {
        "window_days": days,
        "events_total": len(in_window),
        "evaluated": len(results),
        "skipped": dict(skipped),
        "overall": _agg(results),
        "by_scope": {
            "market": _agg([r for r in results if r["symbol"] == MARKET_INDEX["symbol"]]),
            "stock": _agg([r for r in results if r["symbol"] != MARKET_INDEX["symbol"]]),
        },
        "by_group": {
            g: _agg([r for r in results if r["group"] == g])
            for g in ("利多", "利空")
        },
        "by_sector": {},
        "details": results,
    }
    sector_rows = defaultdict(list)
    for r in results:
        for s in r["sectors"]:
            if s.strip():
                sector_rows[s.strip()].append(r)
    summary["by_sector"] = {
        s: _agg(rows) for s, rows in
        sorted(sector_rows.items(), key=lambda x: len(x[1]), reverse=True)[:8]
        if len(rows) >= MIN_GROUP_SAMPLE
    }
    return summary


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


def compute_factor_ic(direction_history: dict, index_closes: list = None) -> dict:
    """各方向维度的 IC（P4-6）：维度分 vs 次日上证收益的 Spearman 秩相关

    direction_history: {YYYY-MM-DD: {"factors": {维度: 分}}}（factor_collector 每交易日一条，
    当日盘中多轮覆盖取最新；维度分与当日收盘近似，配对"当日分 → 次日收益"）
    index_closes: [{"date", "close"}] 升序——调用方复用本轮已拉取的上证 K 线（零额外请求）；
    缺省时现场拉取上证日K。
    返回 {"n": 配对天数, "ic": {维度: IC}, "weights": {维度: 权重}, "updated"}；
    样本 < MIN_IC_DAYS → {"n": n}（调用方等权回退）。
    权重 = max(IC, 0) + IC_FLOOR（收缩地板防早期噪声集中；负 IC 维度只留地板，
    不做反向加权——维度语义固定，负 IC 视为"暂无预测力"而非"反向指标"）。
    """
    if not direction_history:
        return {"n": 0}
    if index_closes is None:
        index_closes = _fetch_kline(MARKET_INDEX["symbol"], 160)
    if not index_closes:
        return {"n": 0}
    # 当日 → 次日收益（%）
    ret = {}
    for i in range(len(index_closes) - 1):
        c0, c1 = index_closes[i]["close"], index_closes[i + 1]["close"]
        if c0:
            ret[index_closes[i]["date"]] = (c1 / c0 - 1) * 100
    # 维度 → (分数序列, 次日收益序列)
    pairs = {}
    n = 0
    for day in sorted(direction_history):
        r = ret.get(day)
        if r is None:
            continue
        rec = direction_history[day] or {}
        facs = rec.get("factors") or {}
        n += 1
        for dim, s in facs.items():
            if isinstance(s, (int, float)):
                bucket = pairs.setdefault(dim, ([], []))
                bucket[0].append(float(s))
                bucket[1].append(r)
    if n < MIN_IC_DAYS:
        return {"n": n}
    ic = {}
    for dim, (xs, ys) in pairs.items():
        if len(xs) >= MIN_IC_DAYS:
            ic[dim] = round(_spearman(xs, ys), 3)
    weights = {dim: round(max(v, 0.0) + IC_FLOOR, 3) for dim, v in ic.items()}
    return {
        "n": n,
        "ic": ic,
        "weights": weights,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def compute_winrate(days: int = 30) -> dict:
    """近 N 天已推事件方向一致率（P4-1：供方向信号卡片标注可信度）

    返回 {"n": 可评估条数, "hit_1": %, "hit_3": %, "hit_5": %}（不足的 horizon 为 None）；
    无事件/无行情 → {"n": 0}。调用方（factor_collector）对 n<10 不展示。
    """
    state = _load_realtime_state()
    events = state.get("pushed_events") or []
    if not events:
        return {"n": 0}
    summary = backtest(events, days=days)
    o = summary.get("overall") or {}
    out = {"n": int(o.get("n") or 0)}
    for n in HORIZONS:
        ev, hit = o.get(f"n_{n}"), o.get(f"hit_{n}")
        out[f"hit_{n}"] = round(hit / ev * 100, 1) if ev and hit is not None else None
    return out


# ============================================================
# 报告
# ============================================================
def _rate_line(agg: dict, label: str) -> str:
    parts = [f"**{label}**（n={agg['n']}）"]
    for n in HORIZONS:
        ev, hit = agg.get(f"n_{n}"), agg.get(f"hit_{n}")
        if ev and hit is not None:
            parts.append(f"后{n}日 {hit}/{ev}（{hit / ev * 100:.0f}%）")
        elif ev:
            parts.append(f"后{n}日 0/{ev}")
        else:
            parts.append(f"后{n}日 无数据")
    return "- " + " | ".join(parts)


def build_report(summary: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    s = summary
    lines = ["# 信号质量回测报告", f"生成时间: {now}", ""]
    lines.append(f"**样本范围**: 近 {s['window_days']} 天已推事件 {s['events_total']} 条，"
                 f"可评估 {s['evaluated']} 条（跳过 {sum(s['skipped'].values())} 条）")
    if s["skipped"]:
        lines.append("跳过原因: " + "、".join(f"{k} {v}" for k, v in s["skipped"].items()))
    # 0 可评估时明确标注冷启动，防止"无数据"被误读为"回测通过/不通过"。
    # dir 字段 2026-08-19 才上线（P0-3），此前历史事件无方向标注；
    # 冷启动期需按强档推送节奏积累，约 2-4 周后分组比率才有统计意义。
    if s["evaluated"] == 0:
        lines.append("")
        lines.append("> ⏳ **样本冷启动中**: 暂无可评估样本，一致率结论待积累。"
                     "dir 字段为 2026-08-19 上线，历史已推事件无方向标注；"
                     "上线后的新推送会自动带 dir 标注进入回测。")
    lines.append("")
    lines.append("## 总体一致率（推送方向 vs 后 N 日行情）")
    lines.append("")
    lines.append(_rate_line(s["overall"], "全部"))
    lines.append("")
    lines.append("## 分组一致率")
    lines.append("")
    lines.append("### 按标的范围")
    lines.append(_rate_line(s["by_scope"]["market"], "大盘级(market→上证)"))
    lines.append(_rate_line(s["by_scope"]["stock"], "个股级"))
    lines.append("")
    lines.append("### 按推送方向")
    lines.append(_rate_line(s["by_group"]["利多"], "利多组（后N日上涨=一致）"))
    lines.append(_rate_line(s["by_group"]["利空"], "利空组（后N日下跌=一致）"))
    lines.append("")
    if s["by_sector"]:
        lines.append(f"### 板块（样本≥{MIN_GROUP_SAMPLE}）")
        lines.append("")
        for sec, agg in s["by_sector"].items():
            lines.append(_rate_line(agg, sec))
        lines.append("")
    # 结论提示（样本门槛内才下结论）
    if s["overall"]["n"] >= MIN_GROUP_SAMPLE:
        lines.append("## 结论提示")
        lines.append("")
        for n in HORIZONS:
            ev, hit = s["overall"].get(f"n_{n}"), s["overall"].get(f"hit_{n}")
            if ev and hit is not None:
                pct = hit / ev * 100
                if pct < 40:
                    lines.append(f"- ⚠️ 后{n}日一致率 {pct:.0f}% 显著低于 50% 基准，"
                                 f"建议收紧推送阈值或复核 LLM 方向判定口径")
                elif pct >= 60:
                    lines.append(f"- ✅ 后{n}日一致率 {pct:.0f}%，信号具备跟踪价值")
        lines.append("")
    lines.append("## 明细（最近 20 条，倒序）")
    lines.append("")
    rows = sorted(s["details"], key=lambda r: r["t"], reverse=True)[:20]
    if rows:
        lines.append("| 时间 | 方向 | 标的 | 后1日 | 后3日 | 后5日 | 标题 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rows:
            def _cell(n):
                v = r.get(f"ret_{n}")
                return f"{v:+.1f}%" if isinstance(v, (int, float)) else "—"
            lines.append(f"| {r['t'][5:16]} | {r['group']} | {r['target']} "
                         f"| {_cell(1)} | {_cell(3)} | {_cell(5)} | {r['title']} |")
    else:
        lines.append("- 无可评估明细")
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
    parser = argparse.ArgumentParser(description="信号有效性回测（P2-3）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"回测窗口天数（默认 {DEFAULT_DAYS}）")
    parser.add_argument("--push", action="store_true", help="推送报告摘要到微信")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    state = _load_realtime_state()
    events = state.get("pushed_events") or []
    if not events:
        print("无已推事件可回测（real_time_state 无 pushed_events）")
        return

    summary = backtest(events, days=args.days)
    report = build_report(summary)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(report)
    print(f"\n[报告已保存] {REPORT_PATH}")

    if args.push:
        # 推送摘要（总体 + 分组，明细过长不推）
        head = report.split("## 明细")[0]
        r = do_push(f"信号质量回测（近{args.days}天）", head)
        print(f"[推送] code={r.get('code', r.get('errcode'))}")


if __name__ == "__main__":
    main()
