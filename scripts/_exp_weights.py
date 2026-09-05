# -*- coding: utf-8 -*-
"""维度权重实验（_exp_weights.py）—— 临时脚本，不入库

背景（数据驱动归因结论）：
 1. `value_erp` 唯一值 {0.0}——估值维是**死因子**，但权重 0.10 仍占分母，
    导致核心分最大只能到 0.90（量纲被压缩 10%），档位线 0.40 却在压缩量纲上判断。
 2. 回撤归因发现：亏损日 `pullback_52w` 已给出 -1.00（最看空）但系统仍 90% 仓位，
    趋势维(0.35)压制落袋维(0.15)。亏损日 ma20_60 均值反而高于盈利日。

本实验：把死权重 0.10 重新分配给各维度（sum 恒=1.0），验证是否改善。
合规路径：权重作为 walk-forward 训练段寻优参数（训练段按卡玛选，测试段评估），
不手工指定生产权重。

候选（均有明确理由，非全空间搜索）：
  W0 基线      趋势.35 量价.20 波动.20 估值.10(死) 落袋.15
  W1 →落袋     趋势.35 量价.20 波动.20 估值.00     落袋.25  （回应归因：落袋被压制）
  W2 →趋势     趋势.45 量价.20 波动.20 估值.00     落袋.15  （= val_w=0，代码已有路径）
  W3 →按比例   趋势.389 量价.222 波动.222 估值.00  落袋.167
  W4 →量价波动 趋势.35 量价.25 波动.25 估值.00     落袋.15
  W5 →落袋激进 趋势.25 量价.20 波动.20 估值.00     落袋.35
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.run_chinext_timing as rt  # noqa: E402
from scripts.run_chinext_timing import backtest_metrics, load_index_sina  # noqa: E402
from scripts.walk_forward_validation import (split_folds, evaluate_test,  # noqa: E402
                                              summarize_oos)
from src.strategy import chinext_timing as ct  # noqa: E402
from src.strategy import index_pe as ipe  # noqa: E402

ORIG_W = rt._default_weights

WEIGHTS = {
    "W0 基线(死权重.10)": {"趋势": 0.35, "量价": 0.20, "波动": 0.20, "估值": 0.10, "落袋": 0.15},
    "W1 →落袋(.25)": {"趋势": 0.35, "量价": 0.20, "波动": 0.20, "估值": 0.00, "落袋": 0.25},
    "W2 →趋势(.45)": {"趋势": 0.45, "量价": 0.20, "波动": 0.20, "估值": 0.00, "落袋": 0.15},
    "W3 →按比例": {"趋势": 0.389, "量价": 0.222, "波动": 0.222, "估值": 0.00, "落袋": 0.167},
    "W4 →量价波动": {"趋势": 0.35, "量价": 0.25, "波动": 0.25, "估值": 0.00, "落袋": 0.15},
    "W5 →落袋激进(.35)": {"趋势": 0.25, "量价": 0.20, "波动": 0.20, "估值": 0.00, "落袋": 0.35},
}


def use(w: dict):
    """monkeypatch 权重（backtest_metrics 内部调用 _default_weights）。"""
    rt._default_weights = lambda val_w=None: dict(w)


def run_oos(df, pe_map, folds):
    rows = []
    st = None
    for train, test in folds:
        r = evaluate_test(df, test, (ct.TIERS, True), pe_map, 0.0, initial_prev=st)
        rows.append(r)
        st = r["final_state"]
    return summarize_oos(rows), rows


def main():
    df = load_index_sina("399006")
    if df is None or df.empty:
        raise SystemExit("399006 日线加载失败")
    pe_map = ipe.load_cy50_pe(PROJECT_ROOT)
    folds = split_folds(df.index, 3, 1)

    print("=== in-sample 全样本（fee=0，严格口径）===")
    print(f"{'变体':<22}{'累计':>9}{'年化':>7}{'夏普':>6}{'回撤':>8}{'换手':>5}{'均仓':>5}")
    in_sample = {}
    for name, w in WEIGHTS.items():
        use(w)
        try:
            m = backtest_metrics(df, fee=0.0, pe_map=pe_map, erp_cap=True)
            in_sample[name] = m
            print(f"{name:<22}{m['total'] * 100:>+8.1f}%{m['cagr'] * 100:>+6.1f}%"
                  f"{m['sharpe']:>6.2f}{m['mdd'] * 100:>7.1f}%{m['switches']:>5}"
                  f"{m['avg_pos'] * 100:>4.0f}%")
        finally:
            rt._default_weights = ORIG_W

    print(f"\n=== walk-forward OOS（训练3年/测试1年，fee=0，{len(folds)} 折）===")
    print(f"{'变体':<22}{'OOS累计':>9}{'年化':>7}{'夏普':>6}{'OOS回撤':>8}{'OOS换手':>8}")
    for name, w in WEIGHTS.items():
        use(w)
        try:
            s, rows = run_oos(df, pe_map, folds)
            sw = sum(r.get("switches", 0) for r in rows)
            print(f"{name:<22}{s['total'] * 100:>+8.1f}%{s['cagr'] * 100:>+6.1f}%"
                  f"{s['sharpe']:>6.2f}{s['mdd'] * 100:>7.1f}%{sw:>8}")
        finally:
            rt._default_weights = ORIG_W

    # ---- 权重纳入训练段寻优（合规路径：训练段按卡玛选，测试段评估）----
    print(f"\n=== walk-forward OOS：权重纳入训练段寻优（按卡玛，{len(WEIGHTS)} 候选）===")
    picks = []
    all_rows = []
    st_state = None
    for i, (train, test) in enumerate(folds, 1):
        train_start, train_end = train
        dft = df.iloc[:train_end]
        best = None
        for name, w in WEIGHTS.items():
            use(w)
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
        use(WEIGHTS[best[0]])
        try:
            r = evaluate_test(df, test, (ct.TIERS, True), pe_map, 0.0,
                              initial_prev=st_state)
            all_rows.append(r)
            st_state = r["final_state"]
        finally:
            rt._default_weights = ORIG_W
        ts = df.index[test[0]].date(), df.index[test[1] - 1].date()
        print(f"折{i} 测试 {ts[0]}~{ts[1]}｜训练段选 {best[0]:<22} 测试段累计 {r['total'] * 100:+.1f}%")
    s = summarize_oos(all_rows)
    sw = sum(r.get("switches", 0) for r in all_rows)
    print(f"\n权重寻优 OOS 复合：累计 {s['total'] * 100:+.1f}% 年化 {s['cagr'] * 100:+.1f}% "
          f"夏普 {s['sharpe']:.2f} 回撤 {s['mdd'] * 100:.1f}% 换手 {sw}")
    print(f"各折选中权重：{picks}")
    print(f"权重稳定性：{'全部一致' if len(set(picks)) == 1 else '跨折漂移（过拟合信号）'}")
    print(f"\n基线 OOS(fee=0) 对照：+68.2% / 夏普0.47 / 回撤-26.4%｜买入持有 +111.2%")


if __name__ == "__main__":
    main()
