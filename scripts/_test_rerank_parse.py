import sys; sys.path.insert(0, r"C:/Users/mxs/Desktop/股市资讯监测agent")
from src.agent.nodes import _safe_parse_json

# 1) 带 ```json 代码块包裹（与 RANK_PROMPT 示例一致，模型常照抄）
fenced = '```json\n{"ranking": [{"title": "达梦数据与海光信息签署战略合作协议", "final_rank": 1, "reason": "信创合作"}, {"title": "龙虎榜: 中际旭创", "final_rank": 2, "reason": "机构净买入"}]}\n```'
r = _safe_parse_json(fenced)
print("[用例1] 代码块包裹 | ranking条数 =", len(r.get("ranking", [])), "| keys =", list(r.keys()))

# 2) 纯 JSON 无包裹
clean = '{"ranking": [{"title": "A", "final_rank": 1, "reason": "x"}]}'
r2 = _safe_parse_json(clean)
print("[用例2] 纯JSON无包裹 | ranking条数 =", len(r2.get("ranking", [])), "| keys =", list(r2.keys()))

# 3) 对照: filtered_news 结构（函数本是为它设计的）
fn = '{"filtered_news": [{"title": "A", "market_impact_score": 7}], "removed_count": 0}'
r3 = _safe_parse_json(fn)
print("[用例3] filtered_news结构 | 条数 =", len(r3.get("filtered_news", [])), "| keys =", list(r3.keys()))

print("\n结论: rerank 用 ranking 结构, _safe_parse_json 仅对纯JSON(用例2)有效, "
      "一旦模型加代码块包裹(用例1)就解析失败 -> 永远降级为原始排序, LLM智能重排实际从未生效")
