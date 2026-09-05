# -*- coding: utf-8 -*-
"""v5.1 新口径（d日快照）权重重寻优（_exp_weights_v51.py）—— 临时脚本，不入库

背景：v5.1 口径变更（信号日 d 用 d 日 14:45 快照，决策 comp[d]）后，
T.50（趋势.50/落袋.10）的合入依据（d-1 口径 OOS +106.1%）失效——
新口径下 T.50 OOS 仅 +83.8%，v4 风格（趋势.35/落袋.25）反升至 +96.8%（固定参数初验）。

本实验（合规路径）：
  1. 固定权重扫描（趋势/落袋互补，量价/波动固定 .20/.20）× walk-forward OOS；
  2. 训练段按卡玛寻优权重（5 候选），测试段评估 → 看权重跨折稳定性；
  3. 最优固定权重的邻域稳健性 + 分折胜率（vs 当前 T.50、vs v4 风格）。

口径：全部为 v5.1 d日快照（backtest_metrics 已改 comp[d]），fee=0（用户拍板）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.run_chinext_timing as rt  # noqa: E402
from scripts.run_chinext_timing import backtest_metrics, load_index_sina  # noqa: E402
from scripts.walk_forward_validation import split_folds, evaluate_test, summarize_oos  # noqa: E402
from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402

ORIG_W = rt._default_weights

# 趋势/落袋互补扫描（量价.20 + 波动.20 固定，合计恒=1.0）
GRID = [
    ("T.30/P.30", 0.30, 0.30),
    ("T.35/P.25", 0.35, 0.25),
    ("T.40/P.20", 0.40, 0.20),
    ("T.45/P.15", 0.45, 0.15),
    ("T.50/P.10", 0.50, 0.10),   # 当前生产 T.50
    ("T.55/P.05", 0.55, 0.05),
]


def mkw(t, p):
    return {"趋势": t, "量价": 0.20, "波动": 0.20, "估值": 0.0, "落袋": p}


def use(w):
    rt._default_weights = lambda val_w=None: dict(w)


def run_oos(df, pe_map, folds):
    rows = []
    st = None
    for train, test in folds:
        r = evaluate_test(df, test, (ct.TIERS, True), pe_map, 0.0, initial_prev=st)
        rows.append(r)
        st = r["final_state"]
    return rows


def main():
    df = load_index_sina("399006")
    if df is None or df.empty:
        raise SystemExit("399006 日线加载失败")
    pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
    folds = split_folds(df.index, 3, 1)

    print("=== 1. 固定权重扫描 × walk-forward OOS（v5.1 d日快照，fee=0）===")
    print(f"{'权重':<12}{'OOS累计':>9}{'年化':>7}{'夏普':>6}{'OOS回撤':>8}{'换手':>6}")
    results = {}
    for name, t, p in GRID:
        use(mkw(t, p))
        try:
            rows = run_oos(df, pe_map, folds)
            s = summarize_oos(rows)
            sw = sum(r.get("switches", 0) for r in rows)
            results[name] = (s, rows)
            print(f"{name:<12}{s['total'] * 100:>+8.1f}%{s['cagr'] * 100:>+6.1f}%"
                  f"{s['sharpe']:>6.2f}{s['mdd'] * 100:>7.1f}%{sw:>6}")
        finally:
            rt._default_weights = ORIG_W

    # ---- 2. 训练段寻优（合规路径）----
    print(f"\n=== 2. 训练段按卡玛寻优权重（{len(GRID)} 候选，固定 tiers=标准+ERP）===")
    picks = []
    all_rows = []
    st_state = None
    for i, (train, test) in enumerate(folds, 1):
        train_start, train_end = train
        dft = df.iloc[:train_end]
        best = None
        for name, t, p in GRID:
            use(mkw(t, p))
            try:
                m = backtest_metrics(dft, fee=0.0, pe_map=pe_map, tiers=ct.TIERS,
                                     erp_cap=True, eval_start=max(60, train_start),
                                     eval_end=train_end - 1)
                if best is None or m["calmar"] > best[1]:
                    best = (name, m["calmar"])
            finally:
                rt._default_weights = ORIG_W
        picks.append(best[0])
        # 测试段用训练段选出的权重
        t_sel, p_sel = GRID[[g[0] for g in GRID].index(best[0])][1:3]
        use(mkw(t_sel, p_sel))
        try:
            r = evaluate_test(df, test, (ct.TIERS, True), pe_map, 0.0,
                              initial_prev=st_state)
            all_rows.append(r)
            st_state = r["final_state"]
        finally:
            rt._default_weights = ORIG_W
        ts = df.index[test[0]].date(), df.index[test[1] - 1].date()
        print(f"折{i} 测试 {ts[0]}~{ts[1]}｜训练段选 {best[0]:<12} 测试段累计 {r['total'] * 100:+.1f}%")
    s = summarize_oos(all_rows)
    sw = sum(r.get("switches", 0) for r in all_rows)
    print(f"\n寻优 OOS 复合：累计 {s['total'] * 100:+.1f}% 年化 {s['cagr'] * 100:+.1f}% "
          f"夏普 {s['sharpe']:.2f} 回撤 {s['mdd'] * 100:.1f}% 换手 {sw}")
    print(f"各折选中：{picks}")
    print(f"权重稳定性：{'全部一致' if len(set(picks)) == 1 else '跨折漂移（过拟合信号）'}")

    # ---- 3. 最优固定权重的分折胜率（vs T.50 当前生产）----
    best_name = max(results, key=lambda k: results[k][0]["total"])
    print(f"\n=== 3. 固定最优 {best_name} vs 当前 T.50/P.10 分折对比 ===")
    _, rows_best = results[best_name]
    _, rows_t50 = results["T.50/P.10"]
    win = sum(1 for b, d in zip(rows_t50, rows_best) if d["total"] > b["total"])
    win_dd = sum(1 for b, d in zip(rows_t50, rows_best) if d["mdd"] > b["mdd"])
    for i, (b, d) in enumerate(zip(rows_t50, rows_best), 1):
        t0 = df.index[b["start"]].date(); t1 = df.index[b["end"] - 1].date()
        print(f"折{i} {t0}~{t1}｜T.50 {b['total'] * 100:+.1f}% → {best_name} "
              f"{d['total'] * 100:+.1f}%（{(d['total'] - b['total']) * 100:+.1f}pp）")
    print(f"\n折间胜率：收益 {win}/9｜回撤 {win_dd}/9")


if __name__ == "__main__":
    main()
