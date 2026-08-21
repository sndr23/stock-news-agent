# A股股市资讯监测 Agent

多源聚合的 A 股重大资讯实时推送系统 —— **重大消息立刻推送到微信，日常流水静默丢弃**（财联社式体验）。

## 快速开始

- **完整部署教程（7 步，约 10 分钟）**：见 [docs/实时推送使用说明.md](docs/实时推送使用说明.md)
- **实时资讯推送入口**：`python scripts/real_time_push.py`（单次）/ `--loop`（本地常驻）/ `--dry-run`（诊断）
- **其余生产入口**（量化因子采集 / 信号回测 / 中信持仓日报）：见下表模块「状态」列
- **云端调度**：cron-job.org 每 30 分钟触发 GitHub Actions（`.github/workflows/realtime-push.yml`）

## 架构

```
多源聚合(8新闻源 + 龙虎榜/业绩预告信号)
  → 三层近似去重 → 事件级指纹增量检测(48h)
  → 规则预筛(重要度≥0.55 或 高信号词)
  → LLM 严格判定(idx对齐/影响分/6档方向/影响范围/事件主体)
  → 强档门槛(仅 bullish/bearish) → 同事件合并 → 跨轮同事件拦截
  → PushPlus/企业微信 微信单条快讯(红涨绿跌)
未判定/未达阈值 → 静默丢弃(记指纹) 或 挂起下轮重试(不记指纹)
状态持久化: GitHub Gist(云端) / logs/real_time_state.json(本地)
```

## 模块

| 模块 | 职责 | 状态 |
|---|---|---|
| `scripts/real_time_push.py` | **生产主流程**（抓取→去重→指纹→预筛→LLM判定→合并→推送→状态） | ✅ 唯一入口 |
| `src/tools/data_fetchers.py` | 8 新闻源 + 2 信号源并行抓取、三层去重、沪深300成分股缓存 | ✅ |
| `src/tools/calculators.py` | 预筛评分/规则方向/板块推断/事件签名去重/展示限流 | ✅ 生产用部分函数 |
| `src/tools/keyword_tables.py` | 共享关键词表（单一事实来源）+ 英文缩写词边界匹配 | ✅ |
| `src/tools/push.py` | PushPlus/企业微信/WxPusher 三后端 + 重试 | ✅ |
| `src/llm_client.py` | LLM 调用与 JSON 容错解析（共享客户端） | ✅ 2026-08-06 抽取 |
| `scripts/factor_collector.py` | 量化因子采集/方向合成/异动推送（P0–P12，含 IC 验证） | ✅ 生产（realtime-factor） |
| `scripts/signal_backtest.py` | 已推事件信号质量回测（后 1/3/5 日一致性 + 分层 IC） | ✅ 生产（signal-backtest） |
| `scripts/push_citic_futures_pos.py` | 中信期货 IF/IH/IC/IM 净持仓日报（全合约聚合口径，Gist 去重） | ✅ 生产（citic-pos-push） |
| `src/strategy/` | **多因子策略层**（数据/因子/评价/合成/风险/优化/回测七层，见下节） | ✅ 2026-08-21 新增（strategy-daily） |
| `scripts/run_strategy.py` | 策略层每日入口：全流程 → 目标持仓 → 调仓建议推送（不下单） | ✅ 生产（strategy-daily） |
| `src/agent/` | LangGraph 批处理管线（历史汇总报告引擎） | ⚠️ DEPRECATED，无生产入口 |
| `daily-review/` | 每日技术面复盘报告（本地生成 HTML） | ⚠️ 独立辅助功能，不入库 |

## 策略层（src/strategy/，2026-08-21）

机构式多因子选股框架（**只出调仓建议，不含自动下单**），日频、沪深300 基准：

```
数据(data.py: 成分/日线/行业 三级降级+增量缓存)
  → 因子(factors.py: 8个量价截面因子 rev5/mom60_5/low_vol/low_turn/size/liq/ppcorr/idio_vol)
  → 预处理(preprocess.py: MAD去极值→标准化→行业+市值中性化)
  → 评价(evaluate.py: RankIC/IR/t值/分层收益/相关矩阵)
  → 合成(synthesize.py: 滚动IC加权，符号自适应+低IC过滤)
  → 风险(risk.py: 简化Barra 行业+风格暴露，LW收缩协方差)
  → 优化(optimizer.py: SLSQP均值方差 个股≤3%/行业偏离≤3%/换手≤30%，排序法降级)
  → 回测(backtest.py: 成本模型25bp/夏普/回撤/IR/分年)
  → 调仓建议(run_strategy.py: 目标持仓diff → 微信推送; MA20仓位叠加)
```

- **无 look-ahead**：全部因子窗口 ≤t；回测有"信号冻结扰动未来价"专项测试
- **入口**：`python scripts/run_strategy.py --dry-run`（打印）/ `--push`（推送+写持仓状态）/ `--backtest` / `--codes`（自定义池）/
  `--link`（启用资讯↔策略三层协同，见下）
- **云端**：`strategy-daily.yml` 每日 18:00（北京），actions/cache 缓存日线增量拉取，已启用 `--link`
- **已知局限**（v1 诚实声明）：成分用当期快照存在幸存者偏差；行业用东财板块非申万；基准行业权重用等权近似

### 资讯↔策略 三层协同（news_link.py，2026-08-21）

资讯流（real_time_push/factor_collector）与策略层浅耦合联动，`--link` 启用后逐层降级、不阻断主链：

| 层 | 方向 | 机制 | 故障降级 |
|---|---|---|---|
| L1 报告织入 | 资讯 → 策略 | 读近48h已推事件，匹配持仓股相关资讯织入日报"持仓股当日相关资讯"板块 | 无匹配则不渲染 |
| L2 事件→alpha | 资讯 → 策略 | 个股级强方向事件对当日 alpha 做温度修正（强多头±1.0σ，仅利多利空）；并把持仓股回写 watchlist.json，让资讯流对持仓股优先放行 | 读失败忽略，无强事件不修正 |
| L3 宏观 overlay | 因子 → 策略 | 聚合 factor_state 快照 + 中信净持仓：IC/IF 深度贴水、两市主力大幅净流出、risk_off 风险收缩、中信全合约大幅净加空时下调目标仓位系数 | 读失败回退 MA20 基准 |
| L3b 中信净持仓 | 持仓 → 策略 | 读 citic-pos 推送落盘的 pos_history，最新交易日全合约净加空 > 2000 手 → 降仓至 0.9 | 无 history 不参与 |

- **只读 Gist 状态、永不写 Gist**，避免与资讯流单写端冲突
- 方向口径复用资讯流 LLM 的 6 档 direction；匹配按代码/名称/去后缀名宽容召回

## 关键设计

- **LLM 严格判定**：预筛通过的全部候选必须由 LLM 判定推/不推（2026-08-03 起无规则直推）；按 idx 精确对齐防漏推；批次失败挂起下轮重试
- **总超时熔断**：单轮 LLM 判定 300s 上限，防多批重试叠加突破 Actions 10min（2026-08-06）
- **双层去重**：事件级指纹（跨轮）+ 推送级事件签名（跨源同事件，LCS/Jaccard/方向守卫）
- **候选溢出挂起**：超上限候选进 pending 下轮重试（最多 3 轮），不再永久漏推（2026-08-06）
- **状态并发安全**：Gist 读-改-写合并，本地与云端并发不互相覆盖
- **词边界匹配**：ST/IPO 等英文缩写词边界感知，防 STorage/nAMD 误命中（2026-08-06）
- **红涨绿跌**：A 股惯例配色 + emoji 表达方向

## 测试

```bash
python -m pytest tests/ -q -m "unit"   # 760+ 纯单元测试（mock，无网络，约 40s）
```

CI 已接入测试门禁（`-m unit`），防止重构/依赖升级回归。

## 依赖

生产运行时**零 `langgraph`/`langchain` 依赖**（2026-08-21 移除——LLM 调用为纯 `httpx`，仅历史批处理包 `src/agent/` 曾引用 langgraph，现已无生产入口）。版本约束见 `requirements.txt` / `requirements-cloud.txt`。

## 环境变量

见 `.ENV` 模板与部署文档：`OPENROUTER_API_KEY`（LLM 判定）、`PUSHPLUS_TOKEN`/`WECOM_WEBHOOK`（推送）、`GIST_TOKEN`/`GIST_ID`（云端状态）、`RT_PUSH_MODE`（strict/standard/loose）、`RT_ALWAYS_ON`、`RT_MAX_CANDIDATES`、`RT_POLL_SECONDS`。

## 历史分析报告

- [docs/项目深度分析报告.md](docs/项目深度分析报告.md)（2026-08-03 首轮）
- [docs/项目深度分析报告_20260806.md](docs/项目深度分析报告_20260806.md)（2026-08-06 二轮 + 复核修正）
- [docs/推送方向误判排查报告.md](docs/推送方向误判排查报告.md)
- [docs/项目深度分析报告_20260821.md](docs/项目深度分析报告_20260821.md)（2026-08-21 三轮深度审查 + 逐条修复）
