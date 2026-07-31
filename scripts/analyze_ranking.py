# filepath: scripts/analyze_ranking.py
"""排序合理性专项分析：读取 trace_final.json，检测
  1) 同 band 内 LLM 评分(market_impact_score) 与综合分(total_score) 倒挂
  2) 主题重复聚类（同一事件的不同角度报道霸占头部）
  3) 关注股位置与时间因子(tf) 失真
  4) 可疑项（低分却标方向性 band）
"""
import json
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent.parent
data = json.load(open(ROOT / "logs/trace_final.json", encoding="utf-8"))
wl = json.load(open(ROOT / "watchlist.json", encoding="utf-8"))
WATCH_STOCKS = set(wl.get("stocks", []))
WATCH_SECTORS = set(wl.get("sectors", []))


def hit_watch(n):
    """是否命中关注列表（个股或板块，模糊匹配）"""
    stocks = list(n.get("affected_stocks", []) or [])
    if n.get("name"):
        stocks.append(n.get("name"))
    for s in stocks:
        if not s:
            continue
        for w in WATCH_STOCKS:
            if s == w or (len(w) >= 2 and len(s) >= 2 and (w in s or s in w)):
                return True
    for sec in n.get("affected_sectors", []) or []:
        for w in WATCH_SECTORS:
            if sec == w or (len(w) >= 2 and (w in sec or sec in w)):
                return True
    return False


print("=" * 100)
print(f"排序合理性分析 | 总条数={len(data)} | 关注股={sorted(WATCH_STOCKS)} 板块={sorted(WATCH_SECTORS)}")
print("=" * 100)

# ---------- 1. 完整排序表 ----------
print("\n【一】完整排序（位置 | band | sc | tot | tf | scope | 关注 | 来源 | 标题）")
for i, n in enumerate(data, 1):
    mark = "★" if hit_watch(n) else " "
    print(f"{i:>2} {mark} [{n.get('impact_band',''):>14}] sc={n.get('market_impact_score',0):>4} "
          f"tot={n.get('total_score',0):.3f} tf={n.get('time_factor',0):.2f} "
          f"{str(n.get('influence_scope','')):>7} {str(n.get('category','')):>4} "
          f"{str(n.get('source','')):<10} | {str(n.get('title',''))[:42]}")

# ---------- 2. 同 band 内 sc vs tot 倒挂 ----------
print("\n【二】同 band 内 LLM 评分与综合分倒挂检测")
print("    （同档内按 tot 已降序；若后项 LLM 分明显更高却排后面，即倒挂）")
by_band = defaultdict(list)
for i, n in enumerate(data):
    by_band[n.get("impact_band")].append((i, n))

inversions = []
for band, items in by_band.items():
    if len(items) < 2:
        continue
    for a_pos in range(len(items)):
        for b_pos in range(a_pos + 1, len(items)):
            ai, an = items[a_pos]
            bi, bn = items[b_pos]
            asc_, bsc_ = an.get("market_impact_score", 0), bn.get("market_impact_score", 0)
            atot, btot = an.get("total_score", 0), bn.get("total_score", 0)
            # an 排在 bn 前面(tot 更高)，但 an 的 LLM 分明显低于 bn
            if asc_ < bsc_ - 0.5 and atot > btot:
                inversions.append((ai + 1, bi + 1, band, asc_, bsc_, atot, btot))

if not inversions:
    print("    未发现明显倒挂（阈值：LLM分差>0.5 且 前者总分更高）")
else:
    for (pa, pb, band, asc_, bsc_, atot, btot) in inversions:
        print(f"    ⚠ #{pa}(sc={asc_},tot={atot:.3f}) 排在 #{pb}(sc={bsc_},tot={btot:.3f}) 前 → "
              f"后者LLM分高 {bsc_-asc_:.1f} 却靠后 [{band}]")
    print(f"    倒挂对数: {len(inversions)}")

# ---------- 3. 主题重复聚类 ----------
print("\n【三】主题重复聚类（同一事件多视角报道霸占头部）")
themes = {
    "美联储/摩根大通推演": ["美联储", "摩根大通", "鸽派", "鹰派", "鲍威尔", "加息", "降息"],
    "特朗普/伊朗地缘": ["伊朗", "特朗普", "原油", "中东", "地缘"],
    "龙虎榜机构买入": ["龙虎榜", "机构净买入"],
    "中际旭创": ["中际旭创"],
    "新易盛": ["新易盛"],
    "深圳/浙江政策": ["深圳", "浙江", "政策", "人工智能", "集成电路"],
}
for theme, kws in themes.items():
    hits = [(i + 1, n) for i, n in enumerate(data)
            if any(k in str(n.get("title", "")) + str(n.get("content", "")) for k in kws)]
    if len(hits) >= 2:
        positions = [p for p, _ in hits]
        print(f"    🔸 {theme}: {len(hits)}条, 位置={positions}")
        for p, n in hits:
            print(f"        #{p} sc={n.get('market_impact_score')} tot={n.get('total_score',0):.3f} | {str(n.get('title',''))[:46]}")

# ---------- 4. 关注股位置 ----------
print("\n【四】关注股命中位置")
watch_hits = [(i + 1, n) for i, n in enumerate(data) if hit_watch(n)]
if not watch_hits:
    print("    无关注股命中")
else:
    for p, n in watch_hits:
        print(f"    ★ #{p} sc={n.get('market_impact_score')} tot={n.get('total_score',0):.3f} "
              f"tf={n.get('time_factor',0):.2f} [{n.get('impact_band')}] | {str(n.get('title',''))[:46]}")

# ---------- 5. 时间因子分布 ----------
print("\n【五】时间因子(tf)分布")
tf_dist = defaultdict(int)
for n in data:
    tf_dist[round(n.get("time_factor", 0), 2)] += 1
for tf in sorted(tf_dist, reverse=True):
    print(f"    tf={tf:.2f}: {tf_dist[tf]}条")

# ---------- 6. 可疑低分方向性 band ----------
print("\n【六】可疑项：低分(sc<=4)却标方向性 band")
suspects = [(i + 1, n) for i, n in enumerate(data)
            if n.get("market_impact_score", 5) <= 4
            and n.get("impact_band") in ("bullish", "bearish", "mildly_bullish", "mildly_bearish")]
if not suspects:
    print("    无")
else:
    for p, n in suspects:
        print(f"    ⚠ #{p} sc={n.get('market_impact_score')} band={n.get('impact_band')} | {str(n.get('title',''))[:40]}")

print("\n" + "=" * 100)
