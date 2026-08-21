# -*- coding: utf-8 -*-
"""
策略层每日入口（run_strategy.py）
====================================================
定位：与 real_time_push（资讯事件流）、factor_collector（状态时序流）并列的
     第三入口——"组合策略流"。多因子选股 → 组合优化 → 调仓建议（只建议，不下单）。

流程（全部基于 ≤t 收盘数据，无 look-ahead）：
数据(增量缓存) → 因子(8个量价截面) → 预处理(去极值/标准化/中性化)
→ 评价(IC/分层，报告用) → 合成(滚动IC加权) → 风险(简化Barra+LW收缩)
→ 优化(SLSQP均值方差：个股≤3%/行业偏离≤3%/换手≤30%) → 调仓建议 markdown → 推送

用法：
  python scripts/run_strategy.py --dry-run      # 全流程，打印报告，不推送不写状态
  python scripts/run_strategy.py --push         # 推送调仓建议 + 写持仓状态
  python scripts/run_strategy.py --backtest     # 历史回测报告（排序法，快）
  python scripts/run_strategy.py --refresh-meta # 强制刷新成分/行业缓存
  python scripts/run_strategy.py --push --backtest  # 回测摘要并入当日推送

风控叠加（overlay）：基准指数 MA20 趋势 → 建议仓位（1.0 / 0.8）；
后续可接中信期货净持仓、基差等 P 系宏观因子（预留 _risk_overlay 注入点）。

状态：data/strategy_state.json（当前持仓权重，供换手约束与下次 diff）。
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

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".ENV")

from src.strategy import data as sdata
from src.strategy import factors as sfact
from src.strategy import preprocess as sprep
from src.strategy import evaluate as seval
from src.strategy import synthesize as ssyn
from src.strategy import risk as srisk
from src.strategy import optimizer as sopt
from src.strategy import backtest as sbt
from src.strategy import news_link as nlink

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_strategy")

STATE_PATH = PROJECT_ROOT / "data" / "strategy_state.json"
HISTORY_START = "20220101"
ALPHA_FACTORS = ["rev5", "mom60_5", "low_vol", "low_turn", "size", "liq", "ppcorr", "idio_vol"]
STYLE_FOR_RISK = ["size", "low_vol", "mom60_5", "low_turn"]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("状态文件损坏，视为空仓: %s", e)
    return {"date": None, "holdings": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_trading_day_now() -> bool:
    try:
        from chinese_calendar import is_workday
        return is_workday(datetime.now())
    except Exception:
        return True


def build_all(codes=None):
    """数据→因子→预处理→合成（dry-run/push/backtest 共用）。"""
    if codes is None:
        codes, _names = sdata.load_universe()
    panel = sdata.load_panels(codes, start=HISTORY_START)
    industry_map = sdata.load_industry_map(panel.codes)
    frames = sfact.factor_frames(panel, industry_map)
    processed = {}
    for name in ALPHA_FACTORS:
        processed[name] = sprep.preprocess_factor(
            frames[name], frames["industry"], frames["lnmv"])
        processed[name] = processed[name].reindex(columns=panel.codes)
    fwd = seval.forward_returns(panel.close, horizon=1)
    composite, weights_hist = ssyn.synthesize_ic_weighted(processed, fwd)
    return panel, industry_map, frames, processed, composite, weights_hist, fwd


def factor_report(processed: dict, fwd: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, df in processed.items():
        s = seval.ic_summary(seval.calc_ic_series(df, fwd))
        rows.append({"因子": name, "IC均值": s["mean"], "IC_IR": s["ir"],
                     "t值": s["t_stat"], "正率": s["pos_ratio"], "样本": s["n"]})
    return pd.DataFrame(rows).set_index("因子").sort_values("IC_IR", ascending=False)


def _risk_overlay(panel, macro: dict = None) -> dict:
    """仓位建议叠加层：v1=MA20趋势 + L3 宏观 overlay（因子流基差/资金流/风险状态）。"""
    idx = panel.index_close["close"]
    ma20 = idx.rolling(20).mean()
    above = bool(idx.iloc[-1] > ma20.iloc[-1]) if len(idx) > 20 else True
    base = 1.0 if above else 0.8
    if macro and "exposure" in macro:
        expo = macro["exposure"]  # 基准 * 宏观系数
    else:
        expo = base
    reasons = []
    if macro and macro.get("reasons"):
        reasons = macro["reasons"]
    reasons.append("基准收盘 > MA20" if above else "基准收盘 < MA20")
    return {"exposure": round(float(expo), 3),
            "reason": "；".join(reasons),
            "ma20": round(float(ma20.iloc[-1]), 1) if len(ma20) else None}


def fmt_pct(x, digits=1) -> str:
    return f"{x * 100:.{digits}f}%"


def build_rebalance_report(target: pd.Series, prev_holdings: dict,
                           names: dict, overlay: dict, ic_tbl: pd.DataFrame,
                           top_tail: pd.DataFrame, latest_date,
                           holding_news: dict = None) -> str:
    lines = []
    lines.append(f"## 多因子策略日报 {latest_date}")
    lines.append("")
    lines.append(f"**仓位建议**：{overlay['exposure']:.0%}（{overlay['reason']}）")
    lines.append("")
    prev = pd.Series(prev_holdings, dtype=float)
    buys, sells = [], []
    tgt = target.copy()
    for code, w in tgt.items():
        delta = w - prev.get(code, 0.0)
        tag = f"{names.get(code, code)}({code})"
        if delta > 0.002:
            buys.append(f"{tag} → {w:.1%}（{(delta) * 100:+.1f}%）")
        elif code in prev.index and abs(delta) <= 0.002 and w > 0:
            pass
    for code, w0 in prev.items():
        if code not in tgt.index or tgt.get(code, 0.0) < 0.002:
            sells.append(f"{names.get(code, code)}({code}) 清仓（原 {w0:.1%}）")
        elif tgt[code] < w0 - 0.002:
            sells.append(f"{names.get(code, code)}({code}) 减至 {tgt[code]:.1%}（原 {w0:.1%}）")
    lines.append(f"**调仓建议**（目标 {len(tgt)} 只，买入 {len(buys)} / 卖出 {len(sells)}）")
    if buys:
        lines.append("- 买入/加仓：")
        lines += [f"  - {b}" for b in buys[:15]]
        if len(buys) > 15:
            lines.append(f"  - …等共 {len(buys)} 条")
    if sells:
        lines.append("- 卖出/减仓：")
        lines += [f"  - {s}" for s in sells[:15]]
        if len(sells) > 15:
            lines.append(f"  - …等共 {len(sells)} 条")
    if not buys and not sells:
        lines.append("- 无调仓（信号与现持仓一致，换手约束内）")
    lines.append("")
    lines.append("**前10大目标持仓**")
    for code, w in tgt.sort_values(ascending=False).head(10).items():
        lines.append(f"- {names.get(code, code)}({code}) {w:.1%}")
    lines.append("")
    # L1 协同：织入当日与持仓股相关的已推资讯（浅耦合，只展示）
    if holding_news:
        shown = 0
        lines.append("**持仓股当日相关资讯**")
        for code, hits in holding_news.items():
            if code not in target.index:
                continue
            for e in hits:
                if shown >= 12:
                    break
                lines.append(f"- {nlink.format_event_line(e)}")
                shown += 1
            if shown >= 12:
                break
        lines.append("")
    w_last = top_tail
    if w_last is not None and len(w_last):
        lines.append("**当期因子权重**：" + "、".join(
            f"{k} {v:+.2f}" for k, v in w_last.items() if abs(v) > 0.01))
        lines.append("")
    if ic_tbl is not None and len(ic_tbl):
        lines.append("**因子近全样本 RankIC**（Top5）")
        lines.append("| 因子 | IC均值 | IR | t值 |")
        lines.append("| --- | --- | --- | --- |")
        for name, r in ic_tbl.head(5).iterrows():
            lines.append(f"| {name} | {r['IC均值']:.3f} | {r['IC_IR']:.2f} | {r['t值']:.1f} |")
        lines.append("")
    lines.append("> 风险提示：量化信号仅供研究参考，不构成投资建议；不含自动下单。")
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


def main():
    ap = argparse.ArgumentParser(description="多因子策略层入口")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送不写状态")
    ap.add_argument("--push", action="store_true", help="推送调仓建议并写状态")
    ap.add_argument("--backtest", action="store_true", help="输出历史回测报告")
    ap.add_argument("--refresh-meta", action="store_true", help="强刷成分/行业缓存")
    ap.add_argument("--codes", type=str, default=None,
                    help="自定义股票池（逗号分隔，默认沪深300全成分；用于研究/冒烟）")
    ap.add_argument("--link", action="store_true",
                    help="启用资讯<->策略三层协同层（L1织入/L2事件修正/L3宏观overlay；默认关）")
    args = ap.parse_args()

    if args.refresh_meta:
        sdata.load_universe(force_refresh=True)
        codes, _ = sdata.load_universe()
        sdata.load_industry_map(codes, force_refresh=True)
        logger.info("成分与行业缓存已强制刷新")
        return

    if not is_trading_day_now():
        logger.info("今日非交易日，跳过")
        return

    logger.info("构建数据与信号…")
    codes = [c.strip().zfill(6) for c in args.codes.split(",")] if args.codes else None
    panel, industry_map, frames, processed, composite, weights_hist, fwd = build_all(codes)
    try:
        _u_codes, names = sdata.load_universe()
    except Exception as e:
        logger.warning("成分表获取失败(%s)，报告内股票名退化为代码", type(e).__name__)
        names = {}
    latest = panel.close.index.max()
    ic_tbl = factor_report(processed, fwd)

    # ---- 三层协同（浅耦合，失败全部降级不阻断主链）----
    macro = None
    holding_news = {}
    link_active = args.link
    if link_active:
        try:
            _st = nlink.load_realtime_state()
            macro = nlink.macro_exposure(nlink.load_factor_state(),
                                         base_exposure=1.0)
            events = nlink.recent_pushed_events(_st, hours=48.0)
            holding_news = nlink.related_news_for_holdings(events, names, names)
            logger.info("协同层已加载：相关资讯 %d 组，宏观 overlay=%s",
                        len(holding_news), macro.get("factor"))
        except Exception as e:
            logger.warning("协同层加载失败，降级为纯策略: %s", type(e).__name__)

    overlay = _risk_overlay(panel, macro=macro)

    state = load_state()
    prev_holdings = state.get("holdings", {})
    w_prev = pd.Series(prev_holdings, dtype=float)

    # 风险模型 + 优化（生产用 mv；协方差取近250日）
    rets = panel.returns()
    style = {k: processed[k] for k in STYLE_FOR_RISK}
    model = srisk.estimate(rets, frames["industry"], style, window=250)
    alpha_today = composite.loc[latest].dropna()

    # L2 协同：个股级强方向事件对当日 alpha 温度修正（仅当协同层激活且有事件）
    if link_active and alpha_today.notna().any():
        try:
            _sigma = float(alpha_today.std())
            events = nlink.recent_pushed_events(
                nlink.load_realtime_state(), hours=48.0)
            corr = nlink.event_alpha_correction(
                events, list(alpha_today.index), names, alpha_sigma=_sigma,
                strong_only=True)
            if corr:
                alpha_today = pd.Series(
                    nlink.apply_alpha_correction(alpha_today.to_dict(), corr))
                logger.info("L2 事件修正应用 %d 只", len(corr))
        except Exception as e:
            logger.warning("L2 事件修正失败，忽略: %s", type(e).__name__)

    ind_row = frames["industry"].loc[latest]
    target = sopt.optimize_mv(alpha_today, model.cov(list(alpha_today.index)),
                              ind_row, w_prev if len(w_prev) else None)
    target = target[target > 0.001].sort_values(ascending=False)

    w_last = weights_hist.loc[latest] if latest in weights_hist.index else None
    report = build_rebalance_report(target, prev_holdings, names, overlay,
                                    ic_tbl, w_last, latest.date(),
                                    holding_news=holding_news)

    bt_summary = ""
    if args.backtest:
        logger.info("运行历史回测（排序法）…")
        res = sbt.run_backtest(composite, panel.close, panel.index_close,
                               frames["industry"], cfg=sbt.BacktestConfig(opt="rank"))
        m = res.metrics
        bt_summary = (f"\n\n**近全样本回测**（{res.nav.index[0].date()} ~ {res.nav.index[-1].date()}）："
                      f"年化 {m.get('ann_return')}% / 夏普 {m.get('sharpe')} / 回撤 {m.get('max_dd')}% / "
                      f"超额 {m.get('excess_ann')}% / IR {m.get('info_ratio')}")
        print("\n=== 分年表现 ===")
        print(res.yearly.to_string())

    print("\n" + "=" * 60)
    print(f"策略日报 {latest.date()}（持仓 {len(target)} 只）")
    print("=" * 60)
    print(report + bt_summary)
    print("\n=== 因子 IC 总表 ===")
    print(ic_tbl.to_string())

    if args.push:
        ok = push_report(report + bt_summary, f"多因子策略日报 {latest.date()}")
        if ok:
            holdings = {c: round(float(w), 5) for c, w in target.items()}
            save_state({"date": str(latest.date()), "holdings": holdings,
                        "exposure": overlay["exposure"]})
            logger.info("已推送并保存持仓状态")
            # L2 反向：持仓股回写 watchlist.json，资讯流对持仓股优先放行
            if link_active and holdings:
                try:
                    wl = {}
                    if nlink.WATCHLIST_PATH.exists():
                        import json as _json
                        wl = _json.loads(nlink.WATCHLIST_PATH.read_text(encoding="utf-8"))
                    merged = nlink.merge_watchlist_holdings(wl, holdings, names)
                    nlink.WATCHLIST_PATH.write_text(
                        _json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info("持仓股已并入 watchlist.json（新增 %d 条）",
                                len(merged["stocks"]) - len(wl.get("stocks", [])))
                except Exception as e:
                    logger.warning("watchlist 回写失败，忽略: %s", type(e).__name__)
    logger.info("完成")


if __name__ == "__main__":
    main()
