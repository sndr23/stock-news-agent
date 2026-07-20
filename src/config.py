"""
配置管理模块
从 .ENV 文件加载 OpenRouter API 配置
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# 清除可能干扰网络请求的代理环境变量
# （本机代理服务未运行时会导致 ConnectionRefused，akshare 和 LLM 调用均受影响）
for _key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(_key, None)

# 加载 .ENV 文件
env_path = Path(__file__).parent.parent / ".ENV"
load_dotenv(env_path)

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
OPENROUTER_MODEL_NAME = os.getenv("OPENROUTER_MODEL_NAME") or os.getenv("openrouter_model_name", "google/gemini-3-flash-preview")
OPENROUTER_BASE_URL = "https://apihub.agnes-ai.com/v1"

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
    print(f"API Key: {OPENROUTER_API_KEY[:20]}...")
    print(f"Model: {OPENROUTER_MODEL_NAME}")
    validate_config()
    print("配置验证通过!")
