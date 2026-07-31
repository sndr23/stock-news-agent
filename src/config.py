"""
配置管理模块
从 .ENV 文件加载 OpenRouter API 配置

副作用守卫：import 时执行的代理清除/日志配置/目录创建仅运行一次，
重复 import（如测试中 reload）不会重复执行，保证测试隔离。
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# 加载 .ENV 文件（先加载，再根据 base_url 决定是否清除代理）
env_path = Path(__file__).parent.parent / ".ENV"
load_dotenv(env_path)

# OpenRouter 配置（提前读取 base_url，用于判断是否需要保留代理）
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL") or os.getenv("openrouter_base_url", "https://openrouter.ai/api/v1")
IS_OPENROUTER_OFFICIAL = "openrouter.ai" in OPENROUTER_BASE_URL

# 副作用守卫：确保代理清除/日志配置仅执行一次，避免测试中重复 import 产生副作用
_config_initialized = getattr(os, "_stock_agent_config_init", False)
if not _config_initialized:
    setattr(os, "_stock_agent_config_init", True)

    # 清除可能干扰网络请求的代理环境变量
    # 注意: OpenRouter 官方 (openrouter.ai) 需要科学上网，必须保留系统代理
    # 仅对当前 Agnes 等国内中转风格端点清除代理，避免代理未运行时 ConnectionRefused
    if not IS_OPENROUTER_OFFICIAL:
        for _key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"]:
            os.environ.pop(_key, None)

    # 日志配置
    LOG_DIR = Path(__file__).parent.parent / "logs"
    LOG_DIR.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8"),
        ]
    )

# OpenRouter 配置
# 大写优先（云端 GitHub Actions 环境变量），小写兜底（本地 .ENV 文件）
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("openrouter_api_key", "")
OPENROUTER_MODEL_NAME = os.getenv("OPENROUTER_MODEL_NAME") or os.getenv("openrouter_model_name", "deepseek/deepseek-v4-flash:free")
# OPENROUTER_BASE_URL 已在上方提前定义（用于代理判断）

# 项目路径配置
PROJECT_ROOT = Path(__file__).parent.parent

# 验证配置
def validate_config():
    """验证配置是否完整"""
    if not OPENROUTER_API_KEY:
        raise ValueError("缺少 openrouter_api_key 配置，请检查 .ENV 文件")
    # 用特征比较代替直接打印(避免GitHub Actions遮蔽secret值)
    _m = OPENROUTER_MODEL_NAME.lower()
    print(f"[config] model: has_agnes={'agnes' in _m} has_gemini={'gemini' in _m} "
          f"has_flash={'flash' in _m} len={len(OPENROUTER_MODEL_NAME)}", flush=True)
    return True

if __name__ == "__main__":
    # 安全: 不打印 API Key 任何片段(前20字符也足以缩小爆破空间/泄露账号特征)
    print(f"API Key: {'已配置' if OPENROUTER_API_KEY else '未配置'}")
    print(f"Model: {OPENROUTER_MODEL_NAME}")
    validate_config()
    print("配置验证通过!")
