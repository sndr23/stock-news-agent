# 排序规则修复报告（2026-07-31）

基于 7 月 31 日 trace_final.json 的 20 条真实数据，复现修复前后排序对比。
修复后排序由 `rank_news` 按新公式复算（含 confidence 加权；不含 LLM 调分与展示层去重，
用于隔离验证排序规则本身的变化）。

## 修复前 → 修复后（Top 12 对照）

| 修复前 | 修复后 | 标题 | 变化说明 |
|---|---|---|---|
| #1 | #2 | MSCI：AI成为2026年上半年市场主导因素 (sector) | market 加成生效后被富达反超 |
| #2 | #1 | 富达：美联储或延至12月才启动加息周期 (market) | **market 级优先达成** |
| #3 | #3 | 科创综指ETF鹏华涨超5% (sector) | — |
| #4 | #4 | 三星电子Q2净利增1344% (sector) | — |
| #5 | #5 | 润六尺科技：华为团队到访 (sector) | — |
| #6 | #12 | 罗氏制药：罗视佳获批 (医药) | **nAMD 误命中 AMD 修复，沉底 6 位** |
| #7 | #10 | 工信部电子信息制造业增加值 (sector) | confidence 权重调整 |
| #8 | #6 | 世贸组织全球贸易增长 (market) | market 加成前置 |
| #9 | #7 | TCL华星跨界半导体封装 (sector) | 倒挂消除 |
| #10 | #8 | 铠侠NAND需求强劲 (sector) | 倒挂消除 |
| #11 | #9 | 铠侠NAND涨价70% (sector) | 倒挂消除 |

## 倒挂检测结果对比

- 修复前：7 对倒挂（`sc=5.0/high` 压 `sc=6.0/medium`，根因 nAMD 假科技加成 + medium 惩罚过重）
- 修复后：0 对倒挂（复算验证：`sc=6.0/medium` 0.5837 > `sc=5.0/high` 0.5655）

## 聚类热度因子（cluster_weight）修复

- 修复前：2-gram Jaccard>0.35，仅标题 → 实际输出恒为 0，0.15 权重因子失效
- 修复后：字符集合 Jaccard>0.35，仅标题 → 预筛 60 条中 26 条非零（旧算法 14 条且最终全 0）
- 实证命中：寒武纪股权激励 5 条簇、行云科技算力协议多视角簇、铠侠 NAND 两条簇
- 误聚控制：模板化公告（海亮/紫金收购）仅产生 cw≈1 低热度，不影响排序质量

## 修改文件清单

| 文件 | 修改 |
|---|---|
| `src/tools/calculators.py` | 新增 `_has_tech_keyword`/`_tech_hit_count`（英文缩写词边界+排除词）；替换 7 处科技词判断；`SCOPE_SCORE_BOOST.market` 0.12→0.20；`CONFIDENCE_WEIGHT.medium` 0.85→0.90 |
| `src/agent/nodes.py` | 预筛科技相关性过滤改词边界匹配；聚类算法 2-gram→字符集合 Jaccard(0.35)；`_SIGNAL_PATTERN` 英文缩写词边界；LLM prompt 第2步文案修正（market 为加权非首要维度）；`SCORE_ADJUST_PROMPT` 输出契约强化 |
| `tests/test_rank.py` | 修正 `_make_news` neutral→bearish 映射 bug；medium 权重断言 0.85→0.90 |
| `tests/test_fix_regression.py` | 新增 10 个回归测试（词边界/聚类/market 加成/confidence） |

## 测试结果

- 全量测试：**214 passed**（204 原有 + 10 新增）
- 修复验证：罗氏制药 `_has_tech_keyword` False（修复前 True，total 0.5655→0.4006）

## 遗留说明

- `trace_final.json` 为修复前产物，其 cluster_weight 字段仍为旧值；下一次真实管线运行（`trace_run.py` live 模式）后将自动产出新值
- 外围央行资讯（欧元区/日本央行）占比偏高属数据源问题，band=neutral 已将其压后，未在本次修复范围
