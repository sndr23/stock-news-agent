# A股股市资讯监测 Agent

多源聚合的 A 股重大资讯实时推送系统 —— **重大消息立刻推送到微信，日常流水静默丢弃**（财联社式体验）。

## 快速开始

- **系统概览与工程现状**：见 [docs/项目深度分析_20260908.md](docs/项目深度分析_20260908.md)
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
| `src/agent/` | LangGraph 批处理管线（历史汇总报告引擎） | ⚠️ DEPRECATED，无生产入口 |
| `daily-review/` | 每日技术面复盘报告（本地生成 HTML） | ⚠️ 独立辅助功能，不入库 |

> **量化择时系统已迁移**：创业板择时（E1 无掩码生产版）现维护于私有库 `sndr23/chinext-timing`，本库仅保留资讯管线依赖的共享工具（`src/strategy/{data,data_freshness,state_io}.py`）。

> **量化择时系统已迁移**：创业板择时（E1 无掩码生产版）现维护于私有库 `sndr23/chinext-timing`，本库仅保留资讯管线依赖的共享工具（`src/strategy/{data,data_freshness,state_io}.py`）。

## 定时推送全景（当前生效）

| 时间 | 推送 | workflow |
|---|---|---|
| 08:45 | 盘前简报 | market-brief |
| 盘中×N | 实时资讯（每30分钟） | realtime-push |
| 盘中×N | 因子采集（盘中15分钟；GitHub schedule 14:15 兜底） | realtime-factor |
| **14:45** | **创业板仓位信号** | **chinext-timing** |
| 15:10 | 盘后复盘 | market-brief |
| 17:00 | 中信持仓日报 | citic-pos-push |
| 手动 | 信号质量回测 | signal-backtest |
| 周日 | 创业板择时质量周报 | chinext-timing-quality |

> 已停用：~~strategy-daily（18:00 选股策略日报）~~

## 关键设计

- **LLM 严格判定**：预筛通过的全部候选必须由 LLM 判定推/不推（2026-08-03 起无规则直推）；按 idx 精确对齐防漏推；批次失败挂起下轮重试
- **总超时熔断**：单轮 LLM 判定 300s 上限，防多批重试叠加突破 Actions 10min（2026-08-06）
- **双层去重**：事件级指纹（跨轮）+ 推送级事件签名（跨源同事件，LCS/Jaccard/方向守卫）
- **候选溢出挂起**：超上限候选进 pending 下轮重试（最多 3 轮），不再永久漏推（2026-08-06）
- **状态并发安全**：Gist 读-改-写合并，本地与云端并发不互相覆盖
- **词边界匹配**：ST/IPO 等英文缩写词边界感知，防 STorage/nAMD 误命中（2026-08-06）
- **红涨绿跌**：A 股惯例配色 + emoji 表达方向

> 准点调度说明：realtime-factor 以 cron-job.org 为主触发，GitHub schedule 在 14:15 提供因子采集
> 兜底；chinext-timing 继续只由 cron-job.org 在 14:45 准点触发。创业板信号不能提前 schedule，
> 否则会先写入 `last_date`，抢跑并抑制正常的 14:45 信号；外部服务失联时需人工恢复或手动触发。

## 工程审查台账（2026-08-22 第三轮核查结论）

| 项 | 结论 | 现状 |
|---|---|---|
| Gist 读-改-写非原子 | ✅ 已达平台最佳实践 | GitHub Gist PATCH 不支持 `If-Match/ETag` 写锁（2026-08-22 实证）；以**写前合并 + fail-stop + workflow concurrency** 三重防护，冲突窗口压缩到单次写入，是平台内最优解 |
| `config.py` import 副作用清代理 | ✅ 已守卫 | 模块级副作用被 `_config_initialized` 守卫包住只执行一次（测试 reload 不重入）；且仅对非 `openrouter.ai` 中转风格清除代理，兼顾官方需科学与国内中转免代理，属有意设计 |
| `run_once` 5b 挂起条目写 `seen[pushed=False]` | ✅ 已修复 | 落选者写 seen 前有 `if not _j.get("judged", True): continue` —— 未判定挂起条目跳过不落指纹，让下轮重入判定，符合"未判定不落指纹"策略 |

以上核查为**只读审查**，无代码改动——三处均已防御到位（详见深度分析报告附注）。

## 测试

```bash
PUSHPLUS_TOKEN=dummy python -m pytest tests/ -q -m "unit" -p no:cacheprovider   # 当前工作区 1394 passed, 15 deselected
```

测试需提供 `PUSHPLUS_TOKEN=dummy`（或 `WECOM_WEBHOOK`）：`scripts/real_time_push.py:3437` 在无推送后端时抛错，部分用例依赖该环境变量。

CI 已接入全量 unit 测试门禁（`python -m pytest tests/ -q -m unit`，step env 提供 `PUSHPLUS_TOKEN=dummy`），防止重构/依赖升级回归。

## 依赖

生产运行时**零 `langgraph`/`langchain` 依赖**（2026-08-21 移除——LLM 调用为纯 `httpx`，仅历史批处理包 `src/agent/` 曾引用 langgraph，现已无生产入口）。版本约束见 `requirements.txt` / `requirements-cloud.txt`。

## 环境变量

见 `.ENV` 模板与部署文档：`OPENROUTER_API_KEY`（LLM 判定）、`PUSHPLUS_TOKEN`/`WECOM_WEBHOOK`（推送）、`GIST_TOKEN`/`GIST_ID`（云端状态，必须同时配置；只配置一个会拒绝运行）、`RT_PUSH_MODE`（strict/standard/loose）、`RT_ALWAYS_ON`、`RT_MAX_CANDIDATES`、`RT_POLL_SECONDS`。

## 分析报告

- **资讯覆盖扩展与硬信号研究**：[docs/资讯覆盖扩展与硬信号事件研究_20260822.html](docs/资讯覆盖扩展与硬信号事件研究_20260822.html)（2026-08-22）
- **数据源加固方案**：[docs/数据源加固方案_20260822.md](docs/数据源加固方案_20260822.md)（2026-08-22）

> 量化相关分析报告（项目深度分析/全链路审计/择时系统说明等）已随量化层迁移出本库，见私有库 `sndr23/chinext-timing`。
