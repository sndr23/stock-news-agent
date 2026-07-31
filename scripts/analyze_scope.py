# filepath: scripts/analyze_scope.py
"""排序哲学验证：band 主序全局有序性 + 宏观近重复簇 + 科技/龙头位置"""
import json
import sys
from pathlib import Path
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

data = json.load(open("logs/trace_final.json", encoding="utf-8"))

SCOPE_LABEL = {"market": "市场级", "sector": "板块级", "stock": "个股级"}

# 1) band 主序全局有序性：rank 序列中 band_priority 应非增
print("=== 1) band 主序全局有序性 ===")
from src.tools.calculators import BAND_PRIORITY
seq = [BAND_PRIORITY.get(n.get("impact_band", "neutral"), 1) for n in data]
violations = [(i, seq[i], seq[i+1]) for i in range(len(seq)-1) if seq[i] < seq[i+1]]
print(f"  排名序 band_priority 序列: {seq}")
print(f"  逆序(高档出现在低档之后)次数: {len(violations)}")
for v in violations[:10]:
    print(f"    pos{v[0]} prio={v[1]} -> pos{v[0]+1} prio={v[2]}")

# 2) 各 scope 的分布
print("\n=== 2) scope 分布 ===")
blocks = defaultdict(list)
for i, n in enumerate(data):
    blocks[n.get("influence_scope", "stock")].append(i)
for sc in ("market", "sector", "stock"):
    if blocks[sc]:
        print(f"  {SCOPE_LABEL[sc]}({sc}): 位置 {min(blocks[sc])}~{max(blocks[sc])} | 共 {len(blocks[sc])} 条")

# 3) 宏观近重复簇（同事件不同报道）
print("\n=== 3) 宏观/主题近重复簇（同 scope=market 且标题关键词高度重合）===")
mkt = [n for n in data if n.get("influence_scope") == "market"]
# 简单聚类：基于共享的 2-gram
def grams(t):
    return set(t[i:i+4] for i in range(len(t)-3)) if len(t) > 3 else set()
clusters = []
used = set()
for i, a in enumerate(mkt):
    if i in used:
        continue
    ga = grams(a.get("title", ""))
    grp = [a]
    used.add(i)
    for j, b in enumerate(mkt):
        if j in used:
            continue
        gb = grams(b.get("title", ""))
        if ga and gb and len(ga & gb) / len(ga | gb) > 0.45:
            grp.append(b)
            used.add(j)
    if len(grp) >= 2:
        clusters.append(grp)
print(f"  检出 {len(clusters)} 个近重复簇（同市场级、标题重合>45%）:")
for c in clusters:
    print(f"  --- 簇(共{len(c)}条) ---")
    for n in c:
        print(f"    · sc={n.get('market_impact_score')} | {n.get('title','')[:42]}")

# 4) 科技/龙头个股位置
print("\n=== 4) 科技/龙头个股资讯落点 ===")
sector_items = [n for n in data if n.get("influence_scope") == "sector"]
print(f"  板块级(sector) 共 {len(sector_items)} 条，排名位置 {[data.index(n) for n in sector_items[:5]]}...")
stock_items = [n for n in data if n.get("influence_scope") == "stock"]
print(f"  个股级(stock) 共 {len(stock_items)} 条，排名位置 {[data.index(n) for n in stock_items[:5]]}...")
# 龙头股（沪深300/科技龙头）是否被识别为 sector
leader_hits = [n for n in data if any(s in (n.get("affected_stocks", []) or []) or s in (n.get("name","") or "") for s in ["中际旭创","新易盛","宁德时代","贵州茅台","工业富联","中芯国际"])]
print(f"  龙头股命中条目: {len(leader_hits)} 条, 其 scope = {Counter(n.get('influence_scope') for n in leader_hits)}")

# 5) 可疑 mislabel（低分却方向性 band）
print("\n=== 5) 低分(score<=4)却标方向性 band 的可疑项 ===")
sus = [n for n in data if (n.get("market_impact_score") or 0) <= 4 and n.get("impact_band") in ("bullish","bearish","mildly_bullish","mildly_bearish")]
for n in sus:
    print(f"  sc={n.get('market_impact_score')} band={n.get('impact_band')} | {n.get('title','')[:40]} | reason={str(n.get('impact_reason',''))[:35]}")

# 6) 同 band 内 score 倒挂检查（scope 加成融入排序键后的合理性验证）
print("\n=== 6) 同 band 内 total 倒挂检查 ===")
from src.tools.calculators import SCOPE_SCORE_BOOST
inversions = 0
for k in range(len(data)-1):
    n1 = data[k]; n2 = data[k+1]
    if n1.get("impact_band") == n2.get("impact_band"):
        # 同 band，应 total+scope_boost 降序
        s1 = n1.get("total_score",0) + SCOPE_SCORE_BOOST.get(n1.get("influence_scope",""), 0)
        s2 = n2.get("total_score",0) + SCOPE_SCORE_BOOST.get(n2.get("influence_scope",""), 0)
        if s1 < s2:
            inversions += 1
print(f"  同 band 内 total+scope_boost 倒挂次数: {inversions} (0=完全合理)")

print("\nDONE")
