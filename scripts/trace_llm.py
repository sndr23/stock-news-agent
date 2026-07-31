# filepath: scripts/trace_llm.py
"""定向诊断 llm_filter: 每批次(输入/解析输出)计数 + merge 匹配分类,
定位 33 条规则降级的真实根因(LLM判噪 vs 标题匹配失败 vs 截断丢失)。"""
import sys, json, time, collections, os
sys.path.insert(0, r"C:/Users/mxs/Desktop/股市资讯监测agent")

from src.agent.state import create_initial_state
from src.agent.nodes import fetch_news_node, prefilter_node, llm_filter_node
from src.agent.nodes import _llm_analyze_batch_structured

CACHE = r"C:/Users/mxs/Desktop/股市资讯监测agent/logs/_prefiltered_cache.json"

# ---- 1. 取 prefiltered (带缓存) ----
if os.path.exists(CACHE) and (time.time() - os.path.getmtime(CACHE)) < 7200:
    print("[cache] 复用 prefiltered 缓存")
    pre = json.load(open(CACHE, encoding="utf-8"))
else:
    print("[fetch] 重新抓取+预筛...")
    t0 = time.time()
    state = create_initial_state("live")
    state.update(fetch_news_node(state))
    state.update(prefilter_node(state))
    pre = state["prefiltered_news"]
    json.dump(pre, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, default=str)
    print(f"[fetch] 完成 {len(pre)} 条 ({time.time()-t0:.0f}s)")

print(f"== prefiltered 共 {len(pre)} 条, 开始分批 LLM 分析 ==")

# ---- 2. 逐批次调用真实 _llm_analyze_batch_structured, 记录 ----
BATCH_SIZE = 10
batches = [pre[i:i+BATCH_SIZE] for i in range(0, len(pre), BATCH_SIZE)]
total_input = 0
total_parsed = 0
parsed_titles = []   # 所有 LLM 返回并成功解析的 title
trunc_batches = 0
for idx, batch in enumerate(batches):
    total_input += len(batch)
    try:
        items = _llm_analyze_batch_structured(batch, deadline=0)
    except Exception as e:
        print(f"  批次{idx}: 全部失败 {e}")
        items = []
    n_parsed = len(items)
    total_parsed += n_parsed
    # 截断判断: 解析数 < 输入数 且 >0 (非全失败) -> 视为截断丢条
    if 0 < n_parsed < len(batch):
        trunc_batches += 1
    for it in items:
        t = (it.get("title","") if isinstance(it, dict) else getattr(it,"title",""))
        parsed_titles.append(t.strip())
    print(f"  批次{idx}: 输入{len(batch)} 解析{n_parsed} "
          f"{'[截断丢条]' if 0<n_parsed<len(batch) else ''}")

print(f"\n== 汇总 ==")
print(f"总输入 {total_input} | LLM 解析返回 {total_parsed} | 差值 {total_input-total_parsed}")
print(f"截断批次(解析<输入): {trunc_batches}/{len(batches)}")

# ---- 3. merge 匹配模拟(复刻 nodes.py 逻辑) ----
pre_titles = [n.get("title","").strip() for n in pre]
parsed_set = set(parsed_titles)
unmatched = [t for t in pre_titles if t not in parsed_set]
print(f"prefiltered 标题数 {len(pre_titles)} | 能在 LLM 返回中找到的 {len(pre_titles)-len(unmatched)} "
      f"| 找不到(将走兜底) {len(unmatched)}")
print("找不到的样本(前12):")
for t in unmatched[:12]:
    print("   -", t[:55])

# 分类: 找不到的里, 有多少是 batch 内被截断丢的, 多少是 LLM 判噪/标题改写
# 若某个 batch 解析<输入, 则该 batch 末尾约 (输入-解析) 条是截断; 其余找不到=判噪/改写
print("\n结论方向: 若 找不到数 ≈ (输入-解析) 则主因为截断; 若远大于则主因为 LLM判噪或标题改写")
