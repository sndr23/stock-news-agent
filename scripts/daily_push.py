# filepath: scripts/daily_push.py
"""
每日资讯推送入口（云端定时任务用）

使用方式:
    # 本地测试
    python scripts/daily_push.py

    # 云端（GitHub Actions 自动调用）
    # 环境变量:
    #   OPENROUTER_API_KEY: LLM API key
    #   OPENROUTER_MODEL_NAME: 模型名
    #   PUSHPLUS_TOKEN: PushPlus token（三选一）
    #   WXPUSHER_TOKEN: WxPusher appToken（三选一，需配合 WXPUSHER_UID）
    #   WXPUSHER_UID: WxPusher 用户 UID（三选一，需配合 WXPUSHER_TOKEN）
    #   WECOM_WEBHOOK: 企业微信群机器人 webhook（三选一）
    #   PUSH_TOP_N: 推送条数（默认 20）
    #   PUSH_TITLE: 推送标题（默认按时段自动生成）
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 确保项目根目录在 path 中（兼容本地运行和云端运行）
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 切换到项目根目录（config.py 依赖相对路径加载 .ENV）
os.chdir(PROJECT_ROOT)

from src.config import OPENROUTER_API_KEY, OPENROUTER_MODEL_NAME
from src.agent.graph import run_agent
from src.tools.push import push_news, push_via_wxpusher, push_via_pushplus, push_via_wecom

# 北京时间
BJT = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)


def _force_exit(code: int):
    """强制退出：flush 日志后立即终止进程，避免 LLM 超时 futures 等后台线程拖着不退出"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    logging.shutdown()
    os._exit(code)


def _send_alert(push_config: dict, alert_msg: str):
    """发送简短告警消息（pipeline失败/推送失败时用）

    与正常推送独立，发一条简短文本消息确保用户能收到失败通知
    """
    title = "A股资讯Agent告警"
    content = f"## {title}\n\n{alert_msg}"
    try:
        if push_config.get("wxpusher_token") and push_config.get("wxpusher_uid"):
            push_via_wxpusher(
                push_config["wxpusher_token"],
                push_config["wxpusher_uid"],
                title, content,
                summary=alert_msg[:20],
            )
        elif push_config.get("pushplus_token"):
            push_via_pushplus(push_config["pushplus_token"], title, content)
        elif push_config.get("wecom_webhook"):
            push_via_wecom(push_config["wecom_webhook"], title, content)
        logger.info(f"告警已发送: {alert_msg[:50]}")
    except Exception as e:
        logger.error(f"告警发送失败: {e}")


def _is_trading_day() -> bool:
    """判断今天是否为A股交易日（排除周末，节假日需手动维护或用 chinese_calendar 库）

    优先使用 chinese_calendar 库（如已安装），否则只排除周末。
    """
    now = datetime.now(BJT)
    # 周末一定不交易
    if now.weekday() >= 5:  # 5=周六, 6=周日
        return False
    # 尝试用 chinese_calendar 判断节假日
    try:
        import chinese_calendar  # type: ignore
        return chinese_calendar.is_workday(now)
    except ImportError:
        # 未安装 chinese_calendar，仅排除周末
        # 节假日可能在 .ENV 或环境变量 HOLIDAY_DATES 中配置（逗号分隔 YYYY-MM-DD）
        holiday_str = os.getenv("HOLIDAY_DATES", "")
        if holiday_str:
            holidays = {d.strip() for d in holiday_str.split(",") if d.strip()}
            if now.strftime("%Y-%m-%d") in holidays:
                return False
        return True


def _build_title() -> str:
    """按当前时段生成推送标题，附带延迟标注（GitHub Actions cron 可能有延迟）

    4个推送时段: 09:00 早盘前 / 12:00 午盘 / 16:00 盘后 / 22:00 晚间
    优先使用 PUSH_SLOT 环境变量（由 cron-job.org 通过 workflow_dispatch inputs 传入），
    这样标题能精确对应触发的时段，不受 GitHub Actions 调度延迟影响。
    """
    now = datetime.now(BJT)
    date_str = now.strftime("%m-%d")

    # slot 映射: 环境变量值 -> (时段名, 预期小时)
    slot_map = {
        "morning":   ("早盘前", 9),
        "noon":      ("午盘", 12),
        "afternoon": ("盘后", 16),
        "night":     ("晚间", 22),
    }

    slot_env = os.getenv("PUSH_SLOT", "").strip().lower()
    if slot_env in slot_map:
        # 外部定时服务触发: 用传入的 slot 精确命名
        slot_name, expected_h = slot_map[slot_env]
        base_title = f"A股{slot_name}资讯 {date_str} {expected_h:02d}:00"
        expected = now.replace(hour=expected_h, minute=0, second=0, microsecond=0)
    else:
        # GitHub 自带 cron 或手动触发: 按当前小时推断时段
        hour = now.hour
        if hour < 11:
            slot_name, expected_h = "早盘前", 9
        elif hour < 14:
            slot_name, expected_h = "午盘", 12
        elif hour < 20:
            slot_name, expected_h = "盘后", 16
        else:
            slot_name, expected_h = "晚间", 22
        base_title = f"A股{slot_name}资讯 {date_str} {expected_h:02d}:00"
        expected = now.replace(hour=expected_h, minute=0, second=0, microsecond=0)

    # 延迟超过30分钟则在标题标注（仅对 GitHub 自带 cron 有意义，
    # cron-job.org 触发时延迟通常 <1分钟）
    delay_min = (now - expected).total_seconds() / 60
    if delay_min > 30:
        base_title += f" (延迟{int(delay_min)}分钟)"

    return base_title


def _already_pushed_recently() -> bool:
    """检查同一时段今天是否已有成功的推送（仅 schedule 兜底触发时调用）

    去重逻辑：
    1. schedule 触发时(无PUSH_SLOT)，按当前小时推断所属时段
    2. 查GitHub API最近4小时内的成功runs
    3. 如果该时段(±2小时窗口)已有成功run，跳过避免重复推送

    仅在 GitHub Actions 环境中生效（有 GITHUB_TOKEN 和 GITHUB_REPOSITORY）。
    """
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return False  # 本地运行或无 token，不跳过

    slot = os.getenv("PUSH_SLOT", "").strip().lower()
    if slot:
        return False  # cron-job.org / 手动触发（有明确 slot），不跳过

    # schedule 触发：按当前小时推断时段，查找同窗口内是否有成功推送
    # 时段窗口：09:00±1h / 12:00±1h / 16:00±1h / 22:00±1h
    now = datetime.now(BJT)
    hour = now.hour
    if hour < 11:
        expected_h = 9    # 早盘窗口 08:00-10:59
    elif hour < 14:
        expected_h = 12   # 午盘窗口 11:00-13:59
    elif hour < 20:
        expected_h = 16   # 盘后窗口 15:00-19:59
    else:
        expected_h = 22   # 晚间窗口 20:00-23:59 + 次日00:00-01:59

    import urllib.request
    import urllib.error

    api_url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=20&status=completed"
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"查询最近 runs 失败(不跳过): {e}")
        return False

    for run in data.get("workflow_runs", []):
        if run.get("conclusion") != "success":
            continue
        created = datetime.fromisoformat(
            run["created_at"].replace("Z", "+00:00")
        ).astimezone(BJT)
        elapsed_min = (now - created).total_seconds() / 60
        # 仅查4小时内的成功推送（覆盖schedule延迟场景）
        if elapsed_min > 240:
            continue
        # 判断该run是否属于同一时段（创建时间在预期小时±2小时内）
        run_hour = created.hour
        hour_diff = abs(run_hour - expected_h)
        # 处理跨午夜（如晚间22点 vs 次日01点，差值21→2）
        hour_diff = min(hour_diff, 24 - hour_diff)
        if hour_diff <= 2:
            logger.info(
                f"同时段已有成功推送(run #{run['run_number']}, "
                f"{int(elapsed_min)}分钟前, run_hour={run_hour}, expected={expected_h})，"
                f"schedule 兜底跳过"
            )
            return True
    return False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # 检查必需的环境变量
    if not OPENROUTER_API_KEY:
        logger.error("缺少 OPENROUTER_API_KEY")
        sys.exit(1)

    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    wxpusher_token = os.getenv("WXPUSHER_TOKEN", "").strip()
    wxpusher_uid = os.getenv("WXPUSHER_UID", "").strip()
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    try:
        top_n = int(os.getenv("PUSH_TOP_N", "20"))
    except ValueError:
        logger.warning(f"PUSH_TOP_N 值无效: {os.getenv('PUSH_TOP_N')}, 使用默认值 20")
        top_n = 20
    title = os.getenv("PUSH_TITLE", "") or _build_title()

    if not pushplus_token and not (wxpusher_token and wxpusher_uid) and not wecom_webhook:
        logger.error("未配置推送后端：需要 PUSHPLUS_TOKEN 或 WXPUSHER_TOKEN+WXPUSHER_UID 或 WECOM_WEBHOOK")
        sys.exit(1)

    # 推送配置（告警用）
    push_config = {
        "pushplus_token": pushplus_token or None,
        "wxpusher_token": wxpusher_token or None,
        "wxpusher_uid": wxpusher_uid or None,
        "wecom_webhook": wecom_webhook or None,
    }

    logger.info(f"开始执行每日推送: {title}")

    # 非交易日跳过推送（避免节假日浪费 LLM 配额）
    if not _is_trading_day():
        logger.info("今日非A股交易日，跳过推送")
        _force_exit(0)

    # schedule 兜底去重：如果2小时内已有成功推送（cron-job.org 已跑过），跳过
    if _already_pushed_recently():
        logger.info("schedule 兜底触发，但近期已有成功推送，跳过避免重复")
        _force_exit(0)

    # 运行 pipeline
    try:
        result = run_agent(data_mode="live", thread_id=f"push_{datetime.now(BJT).strftime('%Y%m%d_%H%M')}")
    except Exception as e:
        logger.error(f"Pipeline 运行失败: {e}", exc_info=True)
        _send_alert(push_config, f"Pipeline运行失败: {str(e)[:80]}")
        _force_exit(1)

    ranked = result.get("ranked_news", [])
    if not ranked:
        logger.warning("Pipeline 返回空结果")
        _send_alert(push_config, "Pipeline返回空结果，今日无资讯可推送")
        _force_exit(0)

    logger.info(f"Pipeline 完成，共 {len(ranked)} 条资讯，推送前 {top_n} 条")

    # 推送
    push_result = push_news(
        ranked,
        pushplus_token=pushplus_token or None,
        wxpusher_token=wxpusher_token or None,
        wxpusher_uid=wxpusher_uid or None,
        wecom_webhook=wecom_webhook or None,
        top_n=top_n,
        title=title,
    )

    # WxPusher 成功 code=1000，PushPlus 成功 code=200，企业微信成功 errcode=0
    if push_result.get("code") in (200, 1000) or push_result.get("errcode") == 0:
        logger.info("推送成功")
        # 保存历史推送记录到 logs/（会被 artifact 上传，方便回溯）
        try:
            history_dir = PROJECT_ROOT / "logs" / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(BJT).strftime("%Y-%m-%d_%H-%M")
            history_path = history_dir / f"push_{ts}.json"
            history_data = {
                "pushed_at": ts,
                "title": title,
                "total_count": len(ranked),
                "pushed_count": min(len(ranked), top_n),
                "news": ranked,
            }
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(history_data, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"历史记录已保存: {history_path.name}")
        except Exception as e:
            logger.warning(f"保存历史记录失败(不影响推送): {e}")
        _force_exit(0)
    else:
        logger.error(f"推送失败: {push_result}")
        # 推送失败告警：尝试发简短消息通知用户
        _send_alert(push_config, f"推送失败({len(ranked)}条资讯未送达): {str(push_result)[:60]}")
        _force_exit(1)


if __name__ == "__main__":
    main()
