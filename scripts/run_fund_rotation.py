# -*- coding: utf-8 -*-
"""
基金轮动信号入口（run_fund_rotation.py）
====================================================
每日 13:45（北京）触发，14:00 前推送微信操作建议，用户 14:30 前手动下单场外基金。

流程：持仓快照(缓存) → 腾讯实时行情盘中估值 → 净值历史动量
      → 仓位层(MA20×宏观overlay) → 滞回建议 → 推送
用法：python scripts/run_fund_rotation.py --dry-run | --push
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

import pandas as pd  # noqa: E402

from src.strategy import fund_data as fd  # noqa: E402
from src.strategy import fund_rotation as fr  # noqa: E402
from src.strategy import news_link as nlink  # noqa: E402
from src.tools.push import push_via_pushplus, push_via_wecom  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fund_rotation")


def push_report(content: str, title: str) -> bool:
    """推送出口（与 run_chinext_timing.push_report 同模式：PushPlus → 企微）。

    此前直接 `from src.tools.push import push_report`（该名字不存在），
    --push 一执行必 ImportError，脚本从未成功推送过。"""
    token = os.getenv("PUSHPLUS_TOKEN")
    webhook = os.getenv("WECOM_WEBHOOK")
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

CONFIG_PATH = PROJECT_ROOT / "config" / "fund_portfolio.json"


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if "funds" not in cfg or not cfg["funds"]:
        raise SystemExit(f"配置缺少 funds: {CONFIG_PATH}")
    return cfg


def position_overlay() -> tuple:
    """仓位层：沪深300 MA20 趋势 × L3 宏观 overlay。返回 (exposure, reasons)。"""
    from src.strategy.data import _load_index_daily
    reasons = []
    base = 1.0
    try:
        idx = _load_index_daily("000300", start="20260101")
        close = idx["close"].tail(60)
        ma20 = close.rolling(20).mean().iloc[-1]
        above = bool(close.iloc[-1] > ma20)
        base = 1.0 if above else 0.8
        reasons.append("沪深300 > MA20，基准满仓" if above else "沪深300 < MA20，基准8成")
    except Exception as e:
        logger.warning("指数趋势获取失败，基准满仓: %s", type(e).__name__)
        reasons.append("指数数据缺失，按满仓基准")
    factor = 1.0
    try:
        macro = nlink.macro_exposure(nlink.load_factor_state(),
                                     base_exposure=1.0,
                                     citic_state=nlink.load_citic_pos_state())
        factor = macro["factor"]
        reasons.extend(macro["reasons"])
    except Exception as e:
        logger.warning("宏观 overlay 获取失败: %s", type(e).__name__)
    return round(base * factor, 3), reasons


def gather_signals(funds: list) -> list:
    """拉取全部基金的信号（盘中估值 + 动量）。"""
    holdings_map = fd.load_holdings_cached(funds, top=10)
    # 收集全部 secid 一次性批量拉行情
    all_secids = [h["secid"] for rows in holdings_map.values() for h in rows]
    quotes = fd.get_quotes(all_secids) if all_secids else {}
    signals = []
    for code in funds:
        rows = holdings_map.get(code) or []
        est = fd.intraday_estimate(rows, quotes)
        nav = fd.fund_nav_returns(code, days=120)
        m = fr.momentum_score(nav, intraday_pct=est["est_pct"])
        signals.append({"code": code, "name": code, "est_pct": est["est_pct"],
                        "covered_w": est["covered_w"], "nav_days": len(nav), **m})
    return signals


def render_report(result: fr.AdviceResult, signals: list,
                  exposure_reasons: list, cfg: dict) -> str:
    """组装推送文本。"""
    now = datetime.now().strftime("%m-%d %H:%M")
    acts = result.actions
    trade = [a for a in acts if a["action"] in ("买入", "卖出", "加仓", "减仓")]
    head = f"【基金轮动 {now}】"
    if trade:
        head += f"今日 {len(trade)} 项操作建议"
    else:
        head += "今日无操作，维持持仓"
    lines = [head, "", f"**总仓位建议：{result.exposure:.0%}**（无债，空出部分持币/货基）"]

    if trade:
        lines.append("")
        lines.append("**操作建议**（15:00 前提交有效）")
        for a in trade:
            lines.append(f"- {a['action']} {a['name']}：{a['detail']}")
    else:
        lines.append("")
        lines.append("持仓均在信号区与缓冲带内，无需换手。")

    lines.append("")
    lines.append("**信号读数**（复合动量降序）")
    for i, s in enumerate(result.scores[:8]):
        trend = "↑" if s["trend_ok"] else "↓"
        hold = "持" if s["code"] in (cfg.get("current_holdings") or {}) else "　"
        lines.append(f"- {i+1}. {s['name']} {hold}{trend} 分数{s['score']:+.1f} "
                     f"20日{s['m20']:+.1f}% 盘中{s['est_pct']:+.2f}%")

    lines.append("")
    lines.append("**仓位依据**")
    for r in exposure_reasons:
        lines.append(f"- {r}")

    lines.append("")
    lines.append("信号=0.5×20日+0.3×60日+0.2×近5日动量；↑=净值在MA20上。"
                 "场外基金申赎费高，滞回设计防抖动；建议仅供参考。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="基金轮动信号（场外基金手动执行版）")
    ap.add_argument("--dry-run", action="store_true", help="仅打印不推送")
    ap.add_argument("--push", action="store_true", help="推送到微信")
    args = ap.parse_args()

    cfg = load_config()
    funds = cfg["funds"]
    holdings = {c: float(w) for c, w in (cfg.get("current_holdings") or {}).items()}

    logger.info("拉取 %d 只基金信号...", len(funds))
    signals = gather_signals(funds)
    for s in signals:
        logger.info("  %s: 分数 %+.1f 盘中 %+.2f%% 净值样本 %d",
                    s["code"], s["score"], s["est_pct"], s["nav_days"])

    exposure, reasons = position_overlay()
    result = fr.build_rotation_advice(
        signals, holdings, exposure=exposure,
        max_positions=int(cfg.get("max_positions", 3)),
        per_fund_cap=float(cfg.get("per_fund_cap", 0.40)),
        buffer_rank=int(cfg.get("buffer_rank", 1)),
        reduce_pct=float(cfg.get("reduce_pct", -10.0)))

    report = render_report(result, signals, reasons, cfg)
    print("\n" + report + "\n")

    if args.push:
        now = datetime.now().strftime("%m-%d")
        if push_report(report, f"基金轮动信号 {now}"):
            logger.info("已推送")
        else:
            logger.warning("推送失败")
    elif not args.dry_run:
        print("（--dry-run 打印 / --push 推送）")


if __name__ == "__main__":
    main()
