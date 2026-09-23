# F1 说明：nested_selection_eval.py 基线断言修复（含窗口固定）

日期：2026-09-24；分支：fix/audit-followup-20260924；基线 commit：0ad4c92

## 1. 任务书要求与实测冲突（必须先读）

F1 要求：v5.1 基线更新为 (0.9645,-0.3101)；v5.2 (1.231,-0.323) 与 buy-and-hold
(1.192,-0.570) 保持不变（"实测仍匹配"）；断言失败信息改指引性；脚本完整跑通断言全 PASS。

实测发现：**裸跑（实时数据链）无法复现任务书里的任何一组数字**。
任务书/审计的数字对应"固定窗口"（3000 根 K 线、末端 2026-09-18、起点 2014-05-23）；
而实时数据链每天滚动（Sina datalen=3000 固定根数），今天的窗口是
3000 根、末端 2026-09-23、起点 2014-05-28。

证据（本目录 F1_first_live_run_fail.txt，及审计 .audit_report/repro_out_oos_baseline.txt）：

| 数据窗口 | v5.2 | v5.1(新代码) | BH |
|---|---|---|---|
| 2014-05-23 → 2026-09-18（任务书口径） | +123.1% | +96.5% | +119.2% |
| 2014-05-28 → 2026-09-23（今天实拉） | +132.4% | +101.9% | +129.2% |

原因：`wfv.split_folds` 按"根数下标"切分折边界（732/244），窗口整体位移数根
K 线会让全部 9 折整体平移，OOS 数字随窗口漂移 ~9pp。审计报告 C3-3/SUMMARY
已披露该敏感性（"+132.4%（2014-05-28 起）vs +123.1%（2014-05-23 起）"）。

## 2. 处置（为什么额外加了"窗口固定"）

若只按任务书改 v5.1 值、保留 v5.2/BH 值：脚本今天裸跑仍会在 v5.2 上断言失败
（实拉窗口 +132.4% ≠ 1.231），验收"断言全 PASS"无法达成；
若把三组值都改成"今天的实拉窗口"值：明天窗口继续滚动又会失败，且直接违背
F1③ 与 F3③ 的固定窗口口径。

因此按 F3③"复现须固定数据窗口"的要求，把脚本运行窗口固定为任务书口径
（3000 根、末端 2026-09-18）：
- 多取 `BASELINE_FETCH_BARS=3400` 根历史（仍走生产免费链 `rct.load_index_sina`，
  失败回退 `opt.load_df()` 原降级链），裁剪到 `BASELINE_WINDOW_END` 之前、
  取末 3000 根；
- 加载期临时使用独立缓存子目录 `data/strategy_cache/nested_selection_eval/`，
  避免与生产 3000 根滚动缓存（同 key、不同 datalen）互相覆盖；用完立即还原；
- 裁剪后不足 3000 根时显式报错给出指引（不静默降级）。

窗口常量（BASELINE_WINDOW_END / BASELINE_WINDOW_BARS / BASELINE_FETCH_BARS）
与 EXPECTED_BASELINES 放在一起，注释写明"更换窗口必须同步更新基线并留档"。
未改动任何策略参数（TIERS/HYST/confirm/因子均未触碰），未改 src/ 生产代码。

## 3. 基线与断言改动（按任务书①②③）

- ① v5.1 参考值 (0.953,-0.236) → **(0.9645,-0.3101)**；注释注明旧值是
  2026-09-18 FIX-20260918-01（commit 6315776，pe_to_cheap_pctile 分位修复）
  之前的旧代码产物及脱钩原因。
- ② 断言失败信息改为指引性：先排查 ①估值分位实现漂移（对比 6315776^）、
  ②固定窗口重建失败/数据窗口漂移（提示看 data: 行与折边界按下标切分），
  并给出复跑命令 `python scripts/nested_selection_eval.py`。
- ③ v5.2 (1.231,-0.323) 与 buy-and-hold (1.192,-0.570) 保持不变。

## 4. 验收证据（F1_run.log / F1_full_output.txt）

命令：`python scripts/nested_selection_eval.py`
- exit_code=0，runtime_seconds=25.326（<900s），data: 3000 bars 2014-05-23 -> 2026-09-18
- replay correctness checks: PASS（全部差值 < 1e-9）
- 基线断言全 PASS：
  - fixed v5.2: diff=0.00042982/0.00044968
  - fixed v5.1: diff=0.00002033/0.00001848
  - buy-and-hold: diff=0.00042195/0.00045988
  - "baseline rounded-value checks: PASS"

## 5. 复现方法（给未来任何人）

1. 脚本自身已固定窗口，直接 `python scripts/nested_selection_eval.py` 即可复现；
2. 若要换窗口研究：同步修改 BASELINE_WINDOW_* 三常量 + EXPECTED_BASELINES，
   并在 .fix_report/ 或 docs 留档新旧数字与原因；
3. 对照旧口径：任务书数字 = 审计重建窗口（见 .audit_report/03_backtest_validation.md）。
