# -*- coding: utf-8 -*-
"""执行滑点对账（_exp_execution_slippage.py）—— 临时脚本，不入库

背景（用户 2026-08-29 明确的执行链路）：
  14:45 推送信号 → **15:00 前手动操作** → 按**当日收盘净值**成交
  → 持有赚的是**明天/未来**的收益，不是当天那 15 分钟。

由此产生的真实摩擦：信号基于 14:45 盘中价，成交却是 15:00 收盘价，
二者之差即"信号→成交"的 15 分钟价差（执行滑点）：

    slip = 当日收盘涨幅 − 14:45 盘中涨幅

只有**换仓**（仓位变化）才产生实际摩擦：单笔成本 = |Δposition| × slip

数据基础（2026-08-29 新增埋点）：
  - intraday_pct：14:45 盘中涨幅（百分点），2026-08-27 审计扩展起记录
  - close_pct：当日收盘涨幅（小数），本次 P1-3 新增，次日回填
  → 因此历史数据无法回算，只能从埋点日起累积。
"""
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".ENV", override=False)


def load_history():
    """从 Gist 读取择时影子 history。"""
    import requests
    tok = os.getenv("GIST_TOKEN", "").strip()
    gid = os.getenv("GIST_ID", "").strip()
    if not (tok and gid):
        raise SystemExit("GIST_TOKEN/GIST_ID 未配置（需先加载 .ENV）")
    url = f"https://api.github.com/gists/{gid}?ts={int(time.time() * 1000)}"
    r = requests.get(url, headers={"Authorization": f"token {tok}",
                                   "Accept": "application/vnd.github+json"},
                     timeout=20)
    r.raise_for_status()
    fobj = (r.json().get("files") or {}).get("chinext_timing_state.json")
    if not fobj:
        return []
    return json.loads(fobj["content"]).get("history") or []


def main():
    import run_chinext_timing as rct

    h = load_history()
    print(f"云端影子 history：{len(h)} 条"
          f"{'（' + h[0]['date'] + ' ~ ' + h[-1]['date'] + '）' if h else ''}")

    paired = sum(1 for r in h
                 if isinstance(r.get("intraday_pct"), (int, float))
                 and isinstance(r.get("close_pct"), (int, float)))
    print(f"可配对样本（同时有 intraday_pct 与 close_pct）：{paired} 条")

    res = rct.execution_slippage(h)
    if res["n"] == 0:
        print("\n⚠ 暂无可测算样本——intraday_pct 自 2026-08-27 埋点，close_pct 自本次"
              " P1-3 埋点，二者需同一交易日配对。")
        print("  随后续交易日运行会自动累积；建议积累 ≥20 个交易日后作结论。")
        return

    print(f"\n=== 执行滑点明细（14:45 信号 → 15:00 成交）===")
    print(f"{'日期':<12}{'盘中%':>8}{'收盘%':>9}{'滑点%':>9}{'Δ仓位':>8}{'摩擦%':>9}")
    for e in res["events"]:
        print(f"{str(e['date']):<12}{e['intraday_pct']:>8.2f}"
              f"{e['close_pct'] * 100:>8.2f}%{e['slip'] * 100:>8.2f}%"
              f"{e['dpos']:>8.2f}{e['cost'] * 100:>8.3f}%")

    print(f"\n=== 汇总 ===")
    print(f"样本 {res['n']} 笔｜平均滑点 {res['mean_slip'] * 100:+.3f}%"
          f"｜平均|滑点| {res['mean_abs_slip'] * 100:.3f}%")
    print(f"最差单笔 {res['worst'] * 100:+.3f}%"
          f"｜累计摩擦（按换仓幅度加权）{res['total_cost'] * 100:+.3f}%")
    if res["n"] < 20:
        print(f"\n⚠ 样本 {res['n']} < 20，仅供参考，不作结论。")


if __name__ == "__main__":
    main()
