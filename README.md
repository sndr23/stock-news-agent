# A股股市资讯监测 Agent

多源聚合的 A 股重大资讯实时推送系统 —— **重大消息立刻推送到微信，日常流水静默丢弃**（财联社式体验）。

## 快速开始

- **完整部署教程（7 步，约 10 分钟）**：见 [docs/实时推送使用说明.md](docs/实时推送使用说明.md)
- **当前唯一生产入口**：`python scripts/real_time_push.py`（单次）/ `--loop`（本地常驻）/ `--dry-run`（诊断）
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
python -m pytest tests/ -q -m "unit"   # 300+ 纯单元测试（mock，无网络，约 6s）
```

CI 已接入测试门禁（`-m unit`），防止重构/依赖升级回归。

## 环境变量

见 `.ENV` 模板与部署文档：`OPENROUTER_API_KEY`（LLM 判定）、`PUSHPLUS_TOKEN`/`WECOM_WEBHOOK`（推送）、`GIST_TOKEN`/`GIST_ID`（云端状态）、`RT_PUSH_MODE`（strict/standard/loose）、`RT_ALWAYS_ON`、`RT_MAX_CANDIDATES`、`RT_POLL_SECONDS`。

## 历史分析报告

- [docs/项目深度分析报告.md](docs/项目深度分析报告.md)（2026-08-03 首轮）
- [docs/项目深度分析报告_20260806.md](docs/项目深度分析报告_20260806.md)（2026-08-06 二轮 + 复核修正）
- [docs/推送方向误判排查报告.md](docs/推送方向误判排查报告.md)
