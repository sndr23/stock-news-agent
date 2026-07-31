# 诊断脚本: 运行 pipeline 并导出 ranked_news 供问题分析(数据层走 1h 缓存, 快速复现)
import sys, json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.graph import run_agent

result = run_agent(data_mode="live", thread_id="diag")
ranked = result.get("ranked_news", [])

out = PROJECT_ROOT / "logs" / "diag_ranked.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(ranked, f, ensure_ascii=False, indent=1, default=str)

print(f"ranked={len(ranked)} raw={len(result.get('raw_news', []))} "
      f"prefiltered={len(result.get('prefiltered_news', []))} filtered={len(result.get('filtered_news', []))}")
for m in result.get("messages", []):
    print("MSG:", str(m.content)[:200])
