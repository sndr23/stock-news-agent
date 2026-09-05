# -*- coding: utf-8 -*-
"""修正层 IC 验门（_exp_modifier_ic.py）—— 临时脚本，不入库

背景：修正层 ±0.30（贴水/资金/情绪/资讯/缠论/旭创）**完全不可回测**，
是最终仓位的"决策黑箱"——实测仅资讯 ±0.06 就能翻转 15.7% 的档位决策。
本脚本用 Gist 影子 history 累积的真实信号，检验各修正项是否真有前向预测力。

方法：对每个修正项 m，计算其分值 vs 前向收益（next_ret/r3/r5/r10）的
Spearman IC。验门标准沿用项目核心层：|IC|≥0.05 且样本≥10 → 保留；
否则建议清零（修正层默认应为 0，只有被证明有效的项才保留）。

⚠ 样本门槛：当前云端仅 5 条（系统 2026-08-22 上线），远不足 10 条门槛。
本脚本在样本不足时**只报告不结论**，避免用小样本得出误导性判断。
"""
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".ENV", override=False)

MODS = ("basis", "flow", "mood", "news", "chan", "stock")
LABEL = {"basis": "贴水", "flow": "资金", "mood": "情绪",
         "news": "资讯", "chan": "缠论", "stock": "旭创"}
GATE_IC = 0.05
GATE_N = 10


def spearman(xs, ys):
    """Spearman 相关（含并列秩平均）。"""
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j < len(order) and vals[order[j]] == vals[order[i]]:
                j += 1
            avg = (i + 1 + j) / 2.0
            for k in range(i, j):
                rk[order[k]] = avg
            i = j
        return rk

    n = len(xs)
    if n < 3:
        return 0.0, n
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = sum((r - mx) ** 2 for r in rx) ** 0.5
    sy = sum((r - my) ** 2 for r in ry) ** 0.5
    return (cov / (sx * sy) if sx > 0 and sy > 0 else 0.0), n


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
    h = load_history()
    print(f"云端影子 history：{len(h)} 条"
          f"{'（' + h[0]['date'] + ' ~ ' + h[-1]['date'] + '）' if h else ''}")
    if len(h) < GATE_N:
        print(f"\n⚠ 样本 {len(h)} 条 < 验门门槛 {GATE_N} 条 —— **不作任何结论**。")
        print("  修正层各修正项当前维持原样（不清零也不提权），等待样本累积。")
        print(f"  按每交易日 1 条估算，约需 {GATE_N - len(h)} 个交易日达到门槛。")

    print("\n=== 修正项覆盖度（非零样本）===")
    for m in MODS:
        nz = sum(1 for r in h if r.get(m))
        print(f"  {LABEL[m]}({m:<6}) 非零 {nz:>3}/{len(h)}")

    print("\n=== 各修正项 vs 前向收益 Spearman IC ===")
    print(f"{'修正项':<12}{'收益窗口':<10}{'样本':>5}{'IC':>8}  判定")
    for ret_key in ("next_ret", "r3", "r5", "r10"):
        for m in MODS:
            pairs = [(r.get(m), r.get(ret_key)) for r in h
                     if isinstance(r.get(m), (int, float))
                     and isinstance(r.get(ret_key), (int, float))]
            if len(pairs) < 3:
                print(f"{LABEL[m]:<12}{ret_key:<10}{len(pairs):>5}{'—':>8}  样本不足")
                continue
            ic, n = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if n >= GATE_N and abs(ic) >= GATE_IC:
                verdict = "✅ 达标（保留）"
            elif n >= GATE_N:
                verdict = "❌ 未达标（建议清零）"
            else:
                verdict = f"⚠ 样本不足（需≥{GATE_N}）"
            print(f"{LABEL[m]:<12}{ret_key:<10}{n:>5}{ic:>+8.3f}  {verdict}")
        print()

    print("验门标准：|IC|≥0.05 且样本≥10。修正层默认应为 0，只有被证明有效的项才保留。")


if __name__ == "__main__":
    main()
