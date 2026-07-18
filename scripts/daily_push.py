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
from src.tools.push import push_news

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


def _build_title() -> str:
    """按当前时段生成推送标题"""
    now = datetime.now(BJT)
    hour = now.hour
    date_str = now.strftime("%m-%d")
    if hour < 12:
        return f"A股盘前资讯 {date_str} 09:00"
    else:
        return f"A股盘后资讯 {date_str} 15:30"


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
    top_n = int(os.getenv("PUSH_TOP_N", "20"))
    title = os.getenv("PUSH_TITLE", "") or _build_title()

    if not pushplus_token and not (wxpusher_token and wxpusher_uid) and not wecom_webhook:
        logger.error("未配置推送后端：需要 PUSHPLUS_TOKEN 或 WXPUSHER_TOKEN+WXPUSHER_UID 或 WECOM_WEBHOOK")
        sys.exit(1)

    logger.info(f"开始执行每日推送: {title}")

    # 运行 pipeline
    try:
        result = run_agent(data_mode="live", thread_id=f"push_{datetime.now(BJT).strftime('%Y%m%d_%H%M')}")
    except Exception as e:
        logger.error(f"Pipeline 运行失败: {e}", exc_info=True)
        # 即使 pipeline 失败也推送错误通知
        push_news(
            [],
            pushplus_token=pushplus_token or None,
            wxpusher_token=wxpusher_token or None,
            wxpusher_uid=wxpusher_uid or None,
            wecom_webhook=wecom_webhook or None,
            top_n=1,
            title=f"{title} - 运行失败",
        )
        _force_exit(1)

    ranked = result.get("ranked_news", [])
    if not ranked:
        logger.warning("Pipeline 返回空结果")
        push_news(
            [],
            pushplus_token=pushplus_token or None,
            wxpusher_token=wxpusher_token or None,
            wxpusher_uid=wxpusher_uid or None,
            wecom_webhook=wecom_webhook or None,
            top_n=1,
            title=f"{title} - 无资讯",
        )
        _force_exit(0)

    logger.info(f"Pipeline 完成，共 {len(ranked)} 条资讯，推送前 {top_n} 条")

    # 推送
    result = push_news(
        ranked,
        pushplus_token=pushplus_token or None,
        wxpusher_token=wxpusher_token or None,
        wxpusher_uid=wxpusher_uid or None,
        wecom_webhook=wecom_webhook or None,
        top_n=top_n,
        title=title,
    )

    # WxPusher 成功 code=1000，PushPlus 成功 code=200，企业微信成功 errcode=0
    if result.get("code") in (200, 1000) or result.get("errcode") == 0:
        logger.info("推送成功")
        _force_exit(0)
    else:
        logger.error(f"推送失败: {result}")
        _force_exit(1)


if __name__ == "__main__":
    main()
