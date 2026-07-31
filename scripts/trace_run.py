# filepath: scripts/trace_run.py
"""
全链路追踪运行器：真实调用管线各节点 (live 模式)，逐阶段捕获中间状态，
并在最后对最终结果做问题扫描。用于实证检验每个环节与最终输出质量。
"""
import sys, json, time, collections
sys.path.insert(0, r"C:/Users/mxs/Desktop/股市资讯监测agent")

from src.config import OPENROUTER_MODEL_NAME, OPENROUTER_BASE_URL
from src.agent.state import create_initial_state
from src.agent.nodes import (
    fetch_news_node, prefilter_node, route_after_prefilter, llm_filter_node, rank_news_node
)
from src.tools.calculators import BAND_PRIORITY, rank_news
from src.agent.state import NO_DATA_SENTINEL

LOG = []
def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)

def jdump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)

# ============ STAGE 0: 配置 ============
log("=" * 70)
log("STAGE 0 | 配置")
log(f"  model={OPENROUTER_MODEL_NAME}  base={OPENROUTER_BASE_URL}")

# ============ STAGE 1: fetch_news ============
t0 = time.time()
state = create_initial_state("live")
r1 = fetch_news_node(state)
state.update(r1)
raw = state["raw_news"]
log("=" * 70)
log(f"STAGE 1 | fetch_news  ({time.time()-t0:.1f}s)")
# 统计各来源
src_counter = collections.Counter(n.get("source", "?") for n in raw)
log(f"  合计抓取(去重后): {len(raw)} 条 | data_status={state['data_status']}")
for s, c in src_counter.most_common():
    log(f"    - {s}: {c}")
# 类别分布
cat_counter = collections.Counter(n.get("category", "?") for n in raw)
log(f"  类别: {dict(cat_counter)}")
log("  样例(前3): " + jdump([n.get("title", "")[:40] for n in raw[:3]]))

# ============ STAGE 2: prefilter ============
t0 = time.time()
r2 = prefilter_node(state)
state.update(r2)
pre = state["prefiltered_news"]
log("=" * 70)
log(f"STAGE 2 | prefilter  ({time.time()-t0:.1f}s)")
log(f"  输入 {len(r1.get('raw_news'))} → 输出 {len(pre)} 条")
# bucket 重算（节点内部已截断，这里看输出里的 prefilter 痕迹已清，改看类别）
catp = collections.Counter(n.get("category", "?") for n in pre)
log(f"  输出类别分布: {dict(catp)}")
# watchlist 命中
try:
    from src.tools.calculators import _load_watchlist
    wl = _load_watchlist().get("stocks", [])
    hits = [n for n in pre if any(w in f"{n.get('title','')} {n.get('content','')}" for w in wl)]
    log(f"  关注列表({len(wl)}只)命中预筛: {len(hits)} 条 -> {[h.get('title','')[:30] for h in hits[:5]]}")
except Exception as e:
    log(f"  watchlist 统计失败: {e}")

# ============ STAGE 3: 路由 ============
route = route_after_prefilter(state)
log("=" * 70)
log(f"STAGE 3 | route_after_prefilter → {route}")

# ============ STAGE 4: llm_filter ============
if route == "go_to_llm":
    t0 = time.time()
    r4 = llm_filter_node(state)
    state.update(r4)
    flt = state["filtered_news"]
    log("=" * 70)
    log(f"STAGE 4 | llm_filter  ({time.time()-t0:.1f}s)")
    # 解析消息里的统计
    for m in r4.get("messages", []):
        log("  MSG: " + str(getattr(m, 'content', m))[:300])
    # band 分布
    bandc = collections.Counter(n.get("impact_band", "?") for n in flt)
    log(f"  输出 {len(flt)} 条 | band 分布: {dict(bandc)}")
    # direction 分布
    dirc = collections.Counter(n.get("impact_direction", n.get("sentiment", "?")) for n in flt)
    log(f"  direction 分布: {dict(dirc)}")
    # 低分却标方向性 band 的可疑项（与护栏阈值对齐：score<4.0 才强制中性；
    # score=4.0 属 mildly 区间，本身是合法轻度方向性，不计入可疑）
    susp = [n for n in flt if n.get("impact_band") in ("bearish", "mildly_bearish", "bullish", "mildly_bullish")
            and (float(n.get("market_impact_score", 5)) < 4.0)]
    log(f"  低分(score<4)却标方向性 band 的可疑项: {len(susp)}")
    for n in susp[:8]:
        log(f"    ⚠ score={n.get('market_impact_score')} band={n.get('impact_band')} | {n.get('title','')[:45]} | reason={str(n.get('impact_reason',''))[:40]}")
else:
    flt = state.get("filtered_news") or state.get("prefiltered_news", [])
    log("=" * 70)
    log(f"STAGE 4 | SKIPPED (route={route})，filtered={len(flt)}")

# ============ STAGE 5: rank_news ============
t0 = time.time()
r5 = rank_news_node(state)
state.update(r5)
ranked = state["ranked_news"]
log("=" * 70)
log(f"STAGE 5 | rank_news  ({time.time()-t0:.1f}s)")
for m in r5.get("messages", []):
    log("  MSG: " + str(getattr(m, 'content', m))[:300])
log(f"  最终排名 {len(ranked)} 条")
log("  --- TOP 12 ---")
for i, n in enumerate(ranked[:12], 1):
    log(f"  #{i:>2} [{n.get('impact_band','?'):>14}] tot={n.get('total_score',0):.3f} "
        f"sc={n.get('market_impact_score',0):.1f} tf={n.get('time_factor',0):.2f} "
        f"prio={n.get('band_priority','?')} | {n.get('title','')[:42]}")

# ============ STAGE 6: 问题扫描 ============
log("=" * 70)
log("STAGE 6 | 最终结果问题扫描")
# 6.1 精确标题重复
title_counter = collections.Counter(n.get("title", "").strip() for n in ranked)
exact_dups = {t: c for t, c in title_counter.items() if c > 1 and t}
log(f"  6.1 精确标题重复: {len(exact_dups)} 组 -> {list(exact_dups.items())[:5]}")

# 6.2 同股聚类（affected_stocks / name / title 含股票名）
stock_counter = collections.Counter()
for n in ranked:
    stocks = n.get("affected_stocks", []) or []
    names = [n.get("name", "")] if n.get("name") else []
    title = n.get("title", "")
    keys = set(stocks) | set(names)
    for k in keys:
        if k:
            stock_counter[k] += 1
    # 也扫描标题里出现的已知股票名（寒武纪等）
top_stocks = stock_counter.most_common(10)
log(f"  6.2 同股出现频次 TOP10: {top_stocks}")

# 6.3 寒武纪专项（用户重点反馈）
cam = [n for n in ranked if "寒武纪" in (n.get("title","") + str(n.get("affected_stocks","")) + str(n.get("name","")))]
log(f"  6.3 寒武纪相关: {len(cam)} 条")
for n in cam:
    log(f"      - {n.get('title','')[:50]} | band={n.get('impact_band')}")

# 6.4 同 band 内 band_priority 逆序（LLM调分是否打乱同band内的分级）
# 注意：band 为排序主键，同 band 内按 total_score+scope加成 排序，
# 跨 band 逆序不会发生（band_priority 是绝对主键），仅检查同 band 内是否有异常。
from src.tools.calculators import BAND_PRIORITY as _BP
viol = 0
prev_band_key = None
prev_prio = None
for n in ranked:
    bp = n.get("band_priority", 3)
    if prev_band_key is not None and bp != prev_band_key:
        prev_band_key = bp
        prev_prio = None
        continue
    if prev_prio is not None and bp > prev_prio:
        viol += 1
    prev_band_key = bp
    prev_prio = bp
log(f"  6.4 band_priority 逆序次数: {viol}  (0=分级有序)")

# 6.5 time_factor 时区校验（不应全为 1.00）
tfs = [n.get("time_factor", 1.0) for n in ranked]
non_one = [t for t in tfs if abs(t - 1.0) > 1e-9]
log(f"  6.5 time_factor: 总数{len(tfs)} 非1.00({len(non_one)}) 范围[{min(tfs):.2f},{max(tfs):.2f}]  "
    f"(时区修复后应有<1.00)")

# 6.6 band 与文本方向一致性（最终仍矛盾的项）
BEAR = {"利空","暴跌","下跌","亏损","处罚","立案","退市","爆雷","违约","承压","下滑","受挫","恶化","巨亏","重挫","闪崩","跌停","大亏","熔断","跌至","走低","下挫","恐慌","抛售","逊于预期","低于预期","不及预期","创新低","回调","搁浅","叫停","制裁","管制","禁运","关税"}
BULL = {"利好","暴涨","上涨","盈利","增持","回购","补贴","扶持","突破","超预期","预增","增长","提振","刺激","宽松","涨停","大涨","走强","回暖","走高","净流入","流入","创新高","涨超","扭亏","中标","签约","扩产","满产","订单饱满"}
def text_dir(t):
    b = sum(1 for k in BEAR if k in t); u = sum(1 for k in BULL if k in t)
    if b and not u: return "bearish"
    if u and not b: return "bullish"
    return "neutral"
mismatch = []
for n in ranked:
    band = n.get("impact_band","")
    reason = str(n.get("impact_reason","") or "")
    chain = str(n.get("analysis_chain","") or "")
    td = text_dir(reason) if reason else "neutral"
    band_dir = "bullish" if "bullish" in band else ("bearish" if "bearish" in band else "neutral")
    if td != "neutral" and band_dir != "neutral" and td != band_dir:
        mismatch.append((n.get("title","")[:40], band, td, reason[:30]))
log(f"  6.6 band↔文本方向最终矛盾: {len(mismatch)} 条")
for mm in mismatch[:10]:
    log(f"      ⚠ {mm}")

# 6.7 兜底/降级项（LLM 未分析）
fallback = [n for n in ranked if "降级" in str(n.get("impact_reason","")) or "规则系统" in str(n.get("impact_reason",""))]
log(f"  6.7 规则降级/兜底项: {len(fallback)} 条")

# 保存完整最终结果与追踪日志
from pathlib import Path
out_dir = Path(r"C:/Users/mxs/Desktop/股市资讯监测agent/logs")
out_dir.mkdir(exist_ok=True)
with open(out_dir / "trace_final.json", "w", encoding="utf-8") as f:
    json.dump(ranked, f, ensure_ascii=False, indent=1, default=str)
with open(out_dir / "trace_log.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(LOG))
log("=" * 70)
log(f"已保存: logs/trace_final.json ({len(ranked)}条) | logs/trace_log.txt")
log("DONE")
