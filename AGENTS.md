# AGENTS.md — Agent Handoff（stock-news-agent / 量化监测agent）

本文件是所有开发 Agent 的第一入口。第一次接手项目时按顺序阅读：

1. `README.md` — 产品定位、运行入口、模块清单与定时推送全景。
2. `docs/status.md` — 当前真实状态和唯一 NEXT。
3. `docs/roadmap.md` — 阶段顺序与阶段目标。
4. `docs/task-board.md` — 当前任务、依赖、Owner、边界和集成状态。
5. `docs/development-plan.md` — 当前 Task 的详细执行合同。
6. `docs/needs-decision.md` — 等待用户决定的问题。
7. 当前 Task 指向的 architecture/spec/ADR/模块文档（`docs/项目深度分析报告_20260822.md`、`docs/创业板择时系统说明_20260822.md`、`docs/数据源加固方案_20260822.md` 等）。

如果文档冲突，以本文件规定的权威职责为准；不要用聊天记录覆盖磁盘和 Git 的当前状态。

## 1. 权威职责

```text
docs/status.md           当前阶段 / 当前事实 / 唯一 NEXT
docs/roadmap.md          阶段顺序 / 阶段目标 / Stage Gate
docs/task-board.md       Task / Owner / 依赖 / Allowed Paths / Acceptance
docs/development-plan.md 复杂 Task 的完整执行合同
docs/needs-decision.md   等待用户决定的问题与恢复条件
Git                      代码、branch、worktree、commit 的真实状态
Tests                    完成证据（tests/ 目录，pytest -m unit）
```

## 2. 项目核心约束（必须遵守）

- **A 股 / 创业板（399006）为主战场**：核心择时信号基于 399006 日线，辅助中际旭创（300308）个股确认；其他指数只作补充维度，不得喧宾夺主。
- **红涨绿跌**：A 股惯例配色，方向表达不得用欧美配色。
- **回测精度门槛**：任何策略改动必须提供回测验证数字；精度不得低于当前基线；结果可信前置条件是"回测 + 预测"双重验证。
- **生产运行时零 `langgraph`/`langchain` 依赖**（2026-08-21 移除）；`src/agent/` 已 DEPRECATED 无生产入口，除非 Task 明确授权否则不得改动。
- **免费数据源永不删除**：付费源（如 Tushare）只是降级链头部，缺失时走原降级路径，永不无信号。
- **不在仓库保存**：OAuth 密码、Bearer Token、签名密钥、SSH 私钥、真实部署配置（见 `.ENV` 模板，真实值只放环境变量）。
- **测试门禁**：`python -m pytest tests/ -q -m "unit"` 必须通过（760+ 用例）。
- **未提交工作保护**：本目录存在未提交的临时脚本（`scripts/_*.py` 等），不得覆盖、reset、stash 或删除；如需处理先询问用户。

## 3. 角色

### Coordinator

- 检查当前阶段与依赖，只领取 `READY` Task。
- 为每个 Task 固定一个 Owner、branch、worktree 和 Base Commit。
- 明确 Allowed Paths、共享文件和 Acceptance。
- 维护 `docs/task-board.md`；共享接口未稳定前不释放依赖任务。

### Worker

- 只实现指定 Task ID，只在自己的 worktree 工作。
- 遵守 Allowed Paths；需要越界时停止并说明原因。
- 不直接修改 status、roadmap、task-board 等全局共享状态。
- 完成代码、测试、必要模块文档和 Git commit。
- 策略/量化相关改动必须附回测或可重复验证证据。

### Integrator

- 串行 review Worker commit，一次只集成一个任务。
- 检查 scope、架构、数据安全、测试和文档。
- 合入 `main` 后重新验收，再更新 Task 和阶段状态。

同一 Agent 可以依次承担三个角色，但不得省略对应检查。

## 4. Git / Worktree

```text
一个 Task = 一个 Owner + 一个 branch + 一个 worktree
```

- Worker 禁止直接在 `main` 开发。
- 新依赖任务必须基于前置任务集成后的最新 `main` 创建。
- 不覆盖、reset、stash 或删除用户和其他 Agent 的未提交工作。
- Coordinator/Integrator 可以在干净的 `main` 维护协调状态文档；业务修改仍在 Task worktree 完成。
- 项目的实际 merge/rebase/cherry-pick 策略以 README 或开发文档为准。

## 5. 默认共享文件

Worker 默认只读：

```text
AGENTS.md
README.md
docs/status.md
docs/roadmap.md
docs/task-board.md
docs/needs-decision.md
requirements*.txt
pytest.ini
config/fund_portfolio.json
watchlist.json
.github/workflows/*.yml
```

确需修改时，必须在 Task Allowed Paths 中明确授权，或交给 Integrator 接线。

## 6. DONE

Task 只有同时满足以下条件才能进入 `DONE`：

1. 实现满足 Acceptance。
2. Task 范围测试通过（`pytest -m unit`）。
3. 必要文档同步。
4. Task branch 已提交。
5. Integrator review 后合入 `main`。
6. `main` 上必要验收通过。
7. Task Board 与 Git 状态一致。

## 7. Worker Handoff

```text
Task: <TASK_ID>
Status: READY_FOR_REVIEW
Branch: <BRANCH>
Commit: <HASH>
Changed:
- ...
Tests:
- ...
Backtest/Validation:   # 策略类 Task 必填
- ...
Notes:
- Integrator 接线事项
- 已知风险 / blocker
```

## 8. 停止条件

- 没有 READY 或可恢复任务；
- Task 与其他 Agent 当前工作冲突；
- Git 状态无法安全继续；
- 需要改变产品方向、核心 Domain、重要架构或重大技术路线；
- Acceptance 无法满足且没有安全的局部修复；
- 回测/验证无法达到精度门槛（策略类）。

需要用户决定时，创建 `ND-xxx`、将 Task 标记为 `BLOCKED`、写清恢复条件并停止。
