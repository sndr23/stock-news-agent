# FIX-STNA-20260924-02 验收索引

分支：`fix/audit-followup-20260924`（基于 main=0ad4c92，**未 push**）
执行日期：2026-09-24 · 输出语言：中文 · 任务书：fix_spec.json

## 四项交付与证据

| 项 | 内容 | 提交 | 证据 |
|---|---|---|---|
| F1 | nested 基线守卫复活：v5.1→(0.9645,-0.3101)、指引性报错、基线固定窗口（3000 根/末端 2026-09-18） | fbf85b6 | F1_run.log、F1_full_output.txt、F1_notes.md、F1_first_live_run_fail.txt |
| F2 | walk_forward_validation 候选网格注释脱钩修正（仅注释） | f2cae9a | git diff |
| F3 | README 测试数字 1394、v5.1 历史口径注、窗口敏感性披露（+ 择时系统说明两节） | 6e4716e | F3_notes.md、pytest_unit.txt |
| F4 | CI 门禁 → tests/ 全量 unit + PUSHPLUS_TOKEN=dummy | a34b7ce | F4_notes.md、F4_ci_step_sim.txt |

## 验收标准对照

1. ✅ `python scripts/nested_selection_eval.py` 完整跑通、断言全 PASS
   （exit=0，runtime 25.326s，replay PASS，三项基线 diff 均 < 0.0005）→ F1_run.log
2. ✅ `PUSHPLUS_TOKEN=dummy python -m pytest tests/ -q -m unit`
   = **1394 passed, 15 deselected**（26.53s，exit=0）→ pytest_unit.txt
3. ✅ `git diff` 仅触及：scripts/nested_selection_eval.py、scripts/walk_forward_validation.py、
   README.md、docs/创业板择时系统说明_20260822.md、.github/workflows/chinext-timing.yml、
   新增 .fix_report/；**src/ 零改动**；.audit_report/、audit_spec.json、fix_spec.json 未入提交
4. ✅ 全部改动在 fix/audit-followup-20260924 分支，F1-F4 各一笔提交；未 push

## 需要知悉的口径偏差（重要）

- 任务书 F1 的数字（v5.1=+96.452% 等）对应**固定数据窗口**（3000 根、末端 2026-09-18）；
  实时数据链每天滚动（固定 3000 根），直接裸跑会因窗口位移整体改变 OOS 数字
  （实测实时窗口 v5.2=+132.4% vs 固定窗口 +123.1%），任务书的"v5.2/BH 保持不变仍匹配"
  仅在固定窗口成立。因此 F1 在按任务书改数字之外，额外把脚本运行窗口固定
  （多取 3400 根裁剪 + 独立缓存子目录），否则"断言全 PASS"无法达成；
  详见 F1_notes.md 第 1-2 节。
- F3 遗留未改位点（共享文件只读/最小化）清单见 F3_notes.md 第"未改"节，
  交 Integrator 决定是否跟进。

## 复跑速查

```bash
python scripts/nested_selection_eval.py                        # F1：应全 PASS
PUSHPLUS_TOKEN=dummy python -m pytest tests/ -q -m unit        # 门禁：1394 passed
git log --oneline main..HEAD                                   # 4 笔提交
```
