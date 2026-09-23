# F4 说明：CI 门禁扩展为全量 unit

日期：2026-09-24；分支：fix/audit-followup-20260924

## 改动（.github/workflows/chinext-timing.yml，测试 step）

```yaml
- name: 运行单元测试门禁（tests/ 全量 unit）
  env:
    PUSHPLUS_TOKEN: dummy
  run: python -m pytest tests/ -q -m unit --no-header -p no:cacheprovider
```

- 旧：只跑 3 个文件（test_chinext_timing/test_chinext_factors/test_chan_light）。
- 新：`tests/` 全量 `-m unit`，一条不漏。
- `--no-header -p no:cacheprovider` 为原 CI 门禁既有旗标，保留不变。

## env 变量依据（先读 real_time_push.py:3437）

```python
pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
if not pushplus_token and not wecom_webhook and not dry_run:
    raise RuntimeError("未配置推送后端: 需要 PUSHPLUS_TOKEN ... 或 WECOM_WEBHOOK ...")
```

即需要 `PUSHPLUS_TOKEN` / `WECOM_WEBHOOK` 二者其一。本 step 显式提供
`PUSHPLUS_TOKEN: dummy`（step env 覆盖 job-level secrets 值）：
- 不依赖 secrets，secrets 未配置（如 fork 场景）时门禁依旧可跑；
- 行为确定，不受真实 token 影响（真实网络调用由测试自行 mock）。

## 时长与依赖

- 本地实测：`25.07s`（1394 passed, 15 deselected，exit=0），远小于 job
  `timeout-minutes: 25`；未使用缓存、未删减任何用例。
- 依赖齐备：tests/ 顶层导入仅 pandas/pydantic/pytest/requests + 仓库模块，
  全部在 requirements-cloud.txt（含 pytest）覆盖范围内；YAML 语法经
  pyyaml 解析校验通过。

## 证据

- `.fix_report/F4_ci_step_sim.txt`：按 CI step 原样本地仿真（exit_code=0，
  1394 passed）。
- 注：真实 GitHub Actions 需 push 后触发（本任务约束不 push），CI 端到端
  验证留待 Hermes/后续统一处理；本地仿真已在同一命令、同一 env 口径下通过。
