import sys, time
sys.path.insert(0, r"C:/Users/mxs/Desktop/股市资讯监测agent")
from src.config import (OPENROUTER_API_KEY, OPENROUTER_MODEL_NAME,
                        OPENROUTER_BASE_URL, IS_OPENROUTER_OFFICIAL)
print("model:", OPENROUTER_MODEL_NAME, "| base:", OPENROUTER_BASE_URL,
      "| official:", IS_OPENROUTER_OFFICIAL, "| key?", bool(OPENROUTER_API_KEY))

# ---- LLM 端点探针 ----
from src.agent.nodes import _call_llm_api
t0 = time.time()
try:
    out = _call_llm_api("你是测试助手。", "只回复两个字：成功", timeout=30, max_retries=1)
    print("LLM_OK:", repr(out[:60]), f"({time.time()-t0:.1f}s)")
except Exception as e:
    print("LLM_FAIL:", repr(str(e)[:200]), f"({time.time()-t0:.1f}s)")

# ---- akshare 抓取探针（东财快讯）----
from src.tools.data_fetchers import _fetch_em_news
t0 = time.time()
try:
    em = _fetch_em_news()
    print("EM_NEWS_OK:", len(em), "条", f"({time.time()-t0:.1f}s)")
except Exception as e:
    print("EM_NEWS_FAIL:", repr(str(e)[:200]), f"({time.time()-t0:.1f}s)")
