# 当前优化审核计划

## 目标

在纯免费数据源约束下，完成本轮生产可靠性审核，修复已确认的问题，并用单元门禁和 dry-run 验收。

## 阶段

- [completed] 补回归测试并修复 Windows 输出、期货回看、指数缓存新鲜度
- [completed] 清理非历史文档中的 Tushare 残留并检查测试重复 mock
- [completed] 执行完整单元测试、dry-run 和 diff 检查
- [completed] 汇总剩余风险与审核结论
- [completed] 完成 SNA-05：免费数据源连续失败健康告警，接入仅采集 workflow
- [completed] 完成 SNA-03：外盘免费回退、PE 稳定性和缓存边界
- [completed] 修正 walk-forward 候选命名与回测接口文档，更新历史报告口径
- [completed] 完成全量单元、编译、静态扫描和生产冒烟验证
- [completed] 继续审核：统一回测基线、复核已知工程缺陷、验证不引入付费数据源

## 本轮继续审核（2026-08-28，第二轮）

- [completed] 为旭创回退复权口径和短历史短路补充失败回归并完成最小修复。
- [completed] 为指数缓存源切换/来源未知场景补充失败回归并禁止跨源量能拼接。
- [completed] 审计生产调用、工作流和文档台账，更新真实测试数字。
- [completed] 执行完整 unit、compileall、diff 检查、dry-run 和主回测复核。

## 本轮约束

- 仅使用免费数据源；不恢复 Tushare 或其他付费接口。
- 不改核心策略权重、档位阈值；策略变更必须有独立留出样本证据。
- 保留用户及前序 Agent 未提交的临时脚本，不 reset、stash、覆盖或删除。

## 本轮继续审核（2026-08-28）

目标：在不调整生产策略参数的前提下，消除当前审计中已确认的口径和工程问题，并给出可复现的回测与风险结论。

执行边界：

- 只使用免费数据源；不恢复、添加或保留 Tushare 生产接口。
- 不覆盖或删除用户/前序 Agent 的未提交临时脚本。
- 策略参数只有在独立留出验证改善后才允许进入生产。

步骤：

- [completed] 核对主回测、实验脚本和文档的指标口径差异。
- [completed] 为已确认的工程缺陷补失败测试并做最小修复。
- [completed] 复跑单元、主回测、walk-forward、编译和静态扫描。
- [completed] 同步当前事实和剩余风险，形成审核结论。

## 2026-08-28 状态持久化复核

- 已按 TDD 覆盖因子 Gist 网络失败、JSON 损坏、云端失败误回退本地三条路径。
- 已修复 `scripts/factor_collector.py`：Gist 读取失败现在抛错并阻止无状态运行；仅“API 成功且文件不存在”允许返回空状态。
- 已修复 `run_chinext_timing.py` 与 `push_citic_futures_pos.py`：写入型状态读取异常不再回退本地；推送后 Gist 写入失败会返回失败并触发任务重试。
- 本地损坏状态（非对象 JSON）统一安全降级为空对象；Gist 损坏内容仍 fail-stop。
- 定向回归：相关 31 passed。

## 2026-08-28 继续审核（本地入口与生产冒烟）

- [completed] 确认择时脚本未加载项目 `.ENV`，补充先失败后通过的本地配置加载回归。
- [completed] 确认并修复基金轮动与择时对账脚本未加载项目 `.ENV`，新增回归并完成先失败后通过验证。
- [completed] 使用项目 `.ENV` 重新执行择时与因子 dry-run，核对今日信号、云端快照时效和健康度。
- [completed] 根据实际冒烟结果修复本地辅助入口配置、对账模式和中信 workflow 最后一次重试等待问题。
- [completed] 最终复跑完整 unit、compileall、diff 检查并汇总剩余风险。

## 2026-08-28 状态读取收口

- 已修复 `factor_collector._load_realtime_state`、`signal_backtest._load_realtime_state`、`real_time_push._load_factor_state` 的 Gist 失败回退本地问题。
- 配置 Gist 时上述增强状态云端唯一；本地状态仅在未配置 Gist 时读取，且非对象根节点降级为空。
- 新增 6 条回归并先验证失败后修复；相关回归集合 `86 passed`。
- 完整 unit 门禁最终为 `986 passed, 21 deselected`；`compileall`、`git diff --check` 和 Tushare 静态扫描均通过。

## 2026-08-28 继续审核（免费回退与原子状态）

- 按 TDD 补充 300308 新浪失败/过期时的腾讯、东财免费回退测试；回退链采用首个新鲜源短路，新浪正常时不额外请求备用源。
- 按 TDD 补充行情缓存根节点类型错误回归；399006、000300、300308 均将非法 DataFrame 缓存视为未命中，不再在 `.empty` 处抛异常。
- 新增 `src/strategy/state_io.py`，四个运行状态写入口改用同目录临时文件 + `fsync` + `os.replace`，避免中断留下半截 JSON。
- 择时本地状态写入失败现在返回 `False`，调用方不会把未持久化信号当作成功。
- 定向回归当前 `220 passed`；完整门禁待本轮最终复跑后更新。

## 2026-08-28 最终收口（响应异常回退）

- 按 TDD 修复 `_fetch_stock_daily()`：东财个股响应非 DataFrame、缺列、日期/数值非法或无有效收盘时按源失败返回 `None`，不改变默认 `hfq` 和旭创显式 `qfq`。
- 按 TDD 修复 `load_index_sina()`：新浪全量指数响应缺少核心字段、日期无效或最新收盘无效时回退 `load_index_daily_full()`，不让损坏列表穿透到核心信号。
- 按 TDD 修复 `_fetch_futures_cffex()`：中金所响应非 DataFrame、空表或缺少主力识别字段时按当日失败继续回看免费链，不在 `.empty` 或列访问处抛异常。
- 新增 4 条异常响应回归；因子采集器测试 `46 passed`，完整 unit 门禁为 `1033 passed, 21 deselected`。
- `compileall`、`git diff --check`、Tushare 静态扫描、因子采集 dry-run、择时 dry-run、主回测和 walk-forward 均通过；策略权重、档位阈值和临时实验脚本未改动。

## 已知错误

| 错误 | 状态 |
|---|---|
| Windows GBK 输出 `UnicodeEncodeError` | 已修复；择时入口和因子采集入口均使用 `errors=replace` |
| 中金所期货首日异常时提前返回 | 已修复；异常日期继续回看最多 4 个自然日 |
| 指数 fallback 可能复用末根交易日过期的缓存 | 已修复；按末根交易日而非文件 TTL 判定 |
| 因子快照有内容但缺失/非法时间戳仍可能改分 | 已修复；无有效当日 `ts` 的非空快照整体失效 |
| 因子采集器接受旧 K 线并与当前报价混合 | 已修复；新浪/腾讯/东财各候选均校验末根交易日 |
| 原始历史恰有 62 根但剔除盘中 bar 后不足 | 已修复；主流程按完整日线根数门禁 |
| 因子采集器 GBK 输出告警符号崩溃 | 已修复；`main()` 统一配置 stdout |
| 外盘/PE 缓存结构损坏可能阻断免费回退 | 已修复；非法根节点、序列和日期统一按空数据降级，并有回归测试 |
| 中金所接口返回非 DataFrame 或缺少主力字段 | 已修复；当前日期按源失败处理并继续回看最多 4 个自然日，异常响应不再穿透解析层 |

## 当前剩余风险

| 风险 | 状态 | 下一步 |
|---|---|---|
| `factor_state` 快照可能因外部定时服务未触发而过期 | 已增加 GitHub schedule 14:15 低频兜底；代码仍会拒绝过期快照参与修正层 | 检查 `realtime-factor.yml` 外部触发、schedule 兜底和最近运行记录，并确认快照按交易日更新 |
| `chinext-timing` 外部准点服务失联 | 保持仅 cron-job.org 14:45 主触发，未添加提前 schedule，避免 `last_date` 抢跑主信号 | 检查 14:45 外部触发记录；失联时人工恢复或手动触发 |
| Yahoo/Stooq 免费接口限流或浏览器验证 | 已有独立回退和按序列降级；无稳定 SLA | 仅观察可用性，不把偶发成功当成稳定性证明 |
| 核心策略 OOS 跑输买入持有且参数跨折漂移 | 已知、接受 | 留出样本/影子期前不改权重和档位线 |

## 本轮结论

- 当前实现已完成本轮免费数据源异常响应加固，当前无新的 READY 代码任务。
- 下一步仍是外部核验 `realtime-factor.yml` 的 14:15 兜底、`chinext-timing.yml` 的 14:45 主触发和 `factor_state` 按交易日更新；本地无法替代云端调度记录。

## 2026-08-28 Gist 读取失败 fail-stop 收口

- [completed] 按 TDD 验证并修复 Gist 已配置但本地读取失败时回退旧状态的问题。
- [completed] 定向回归 `211 passed`；完整 unit 门禁 `1093 passed, 21 deselected`。
- [completed] `compileall`、`git diff --check`、生产路径免费源/Tushare 静态扫描通过。
- [pending] 外部调度实际运行记录仍需用户在 GitHub Actions / cron-job.org 控制台核验；硬编码 PAT 需先撤销轮换后再清理临时审计脚本。

## 2026-08-28 本轮最终验收

- [completed] 全量 unit 门禁：`1048 passed, 21 deselected`。
- [completed] `compileall`、`git diff --check`、生产回测、Gist 对账和免费源静态扫描通过。
- [completed] 今日生产冒烟：399006 最新完整日线至 `2026-08-27`，目标仓位 `0%`、`risk_off`；因子健康度 `12/14`。
- [completed] 线上 Actions / cron-job.org 实际触发记录仍需用户在控制台确认，本地无法替代云端证据。

## 2026-08-28 缓存边界复核

- [completed] 为个股首次全量路径增加末根新鲜度失败回归，阻止过期免费响应写入缓存或进入选股面板。
- [completed] 为统一缓存校验增加 DatetimeIndex 和最新日期必要字段有效性门禁，阻止字符串索引增量计算异常及最新坏行穿透。
- [completed] 定向数据层回归 `42 passed`；完整 unit 门禁 `1048 passed, 21 deselected`；`compileall`、`git diff --check` 通过。
- [completed] 生产 dry-run：择时仍为 399006 截至 `2026-08-27`、`0%`、`risk_off`；因子采集本机代理下 `11/14`，行业资金流和期权 PCR 按设计降级。

## 2026-08-28 第三轮免费增强维度审计

- [completed] 审查事件、融资融券、外盘、估值和因子采集器对异常免费响应的降级边界。
- [completed] 审查生产入口与 workflow 的重复触发、状态写入和过期状态保护。
- [completed] 对新增问题按 TDD 补回归并做最小修复。
- [completed] 复跑全量 unit、编译、差异检查和生产 dry-run，更新最终台账；最终为 `1081 passed, 21 deselected`。

## 2026-08-28 第四轮可靠性审核收口

- [completed] 为中金所 XML 截断、字段缺失、非法数值和目标日部分品种缺失补充失败回归。
- [completed] 修复中信持仓日报：坏品种按源失败，目标日必须 IF/IH/IC/IM 四品种完整后才允许去重、推送和写入历史；中信近 5 日展示也只消费完整日。
- [completed] 为新浪指数全量响应补充 `amount=NaN`/负值回归，非法成交额整段回退免费短链，禁止坏量能进入量价因子。
- [completed] 修复中信日报 Windows 非 UTF-8 stdout 输出崩溃，补入口回归并完成中信 dry-run。
- [completed] 最终验证：`1088 passed, 21 deselected`、`compileall`、`git diff --check`、免费源静态扫描、择时/因子/中信持仓 dry-run、主回测和 walk-forward 均通过。
- [pending] 外部调度实际运行记录与未提交审计脚本中的硬编码 PAT 仍需在 GitHub / cron-job.org 控制台和凭据管理侧处理。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `tests/test_factor_e2e.py::test_run_once_full_flow` 中核心技术面 `available=False` | 1 | 测试夹具生成了无效的 2026-08 月内日期序列，且不符合新的 60 根完整日线契约；改为 65 根连续有效工作日 K 线。 |
