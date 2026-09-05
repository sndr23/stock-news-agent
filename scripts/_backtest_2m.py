# -*- coding: utf-8 -*-
"""近二个月回测详情（_backtest_2m.py）—— 临时探测脚本，不入库
口径与官方 run_backtest 完全一致（load_index_sina + backtest_metrics，
不注入估值 erp，生产一致），但逐日记录核心分/仓位/收益/净值，截近 44 个交易日
（约 2 个月）输出 HTML 明细报告（策略 vs 买入持有）。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy.data import load_index_sina

SYMBOL = "399006"
WIN = 44  # 近 2 个月（约 44 个交易日）信号日
FEE = 0.0


def main():
    df = load_index_sina(SYMBOL)
    if df is None or df.empty:
        raise SystemExit("399006 数据加载失败")
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    dates = df.index
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    n = len(closes)
    start = 60

    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    comp = cf.dimension_score(signals, {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
                                        "估值": 0.10, "落袋": 0.15})

    prev = {"position": 0.0, "pending": None}
    nav, peak = 1.0, 1.0
    navs, pos_daily, ret_daily, comp_daily, cap_daily = [], [], [], [], []
    tiertxt_daily, changed_daily = [], []

    def tier_text(p):
        if p >= 0.999:
            return "满仓100%"
        if p > 0.85:
            return "九成90%"
        if p > 0.4:
            return "六成底仓"
        return "空仓0%"

    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        dec = ct.decide_position(comp[d], caps["cap"], prev, tiers=ct.TIERS)
        if dec["changed"]:
            nav *= (1 - FEE * abs(dec["position"] - prev["position"]))
        prev = {"position": dec["position"], "pending": dec["pending"]}
        r = closes[d + 1] / closes[d] - 1.0
        nav *= (1 + dec["position"] * r)
        peak = max(peak, nav)
        navs.append(nav)
        pos_daily.append(dec["position"])
        ret_daily.append(dec["position"] * r)
        comp_daily.append(comp[d])
        cap_daily.append(caps["cap"])
        tiertxt_daily.append(tier_text(dec["position"]))
        changed_daily.append(dec["changed"])

    # 窗口 = 最后 WIN 个信号日
    w_start = len(navs) - WIN
    seg = list(range(w_start, len(navs)))
    d0 = date_strs[w_start + start]
    d1 = date_strs[-2]

    # 窗口收益
    nav0 = navs[w_start - 1] if w_start > 0 else 1.0
    seg_nav = [navs[i] / nav0 for i in seg]
    seg_strat = seg_nav[-1] - 1.0
    # 窗口持有收益：从 w_start 信号日的下一日（即 seg 首日收益对应当日）起
    bh0 = closes[w_start + start]
    bh_seg = [closes[si + start + 1] / bh0 - 1.0 for si in range(w_start, len(navs))]
    seg_bh = bh_seg[-1]

    # 窗口内换仓次数 + 每日表
    switches = sum(1 for i in seg if changed_daily[i])
    avg_pos = sum(pos_daily[i] for i in seg) / WIN

    rows = []
    for k in range(seg[0], len(navs)):
        i = k - w_start
        rows.append({
            "date": date_strs[k + start],
            "comp": comp_daily[k],
            "cap": cap_daily[k],
            "tier": tiertxt_daily[k],
            "changed": changed_daily[k],
            "pos": pos_daily[k],
            "skret": ret_daily[k],
            "idxret": closes[k + start + 1] / closes[k + start] - 1.0,
            "nav": seg_nav[i],
        })

    # 全区间汇总（对照）
    total_all = navs[-1] - 1.0

    html = _render(d0, d1, seg_strat, seg_bh, switches, avg_pos, rows, closes,
                   seg_nav, w_start, start, total_all)
    out = PROJECT_ROOT / "docs" / "近二月回测详情.html"
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}")
    print(f"窗口 {d0} ~ {d1}：策略 {seg_strat:+.2%} / 持有 {seg_bh:+.2%} / "
          f"换仓 {switches} 次 / 平均仓位 {avg_pos:.0%}")
    print(f"全区间(核心层)累计 {total_all:+.1%}")


def _render(d0, d1, seg_strat, seg_bh, switches, avg_pos, rows, closes,
            seg_nav, w_start, start, total_all):
    hdr = "".join(
        f"<tr><td>{r['date']}</td>"
        f"<td class='{'pos' if r['comp']>0 else 'neg' if r['comp']<0 else ''}'>"
        f"{r['comp']:+.2f}</td>"
        f"<td>{r['cap']:.0%}</td>"
        f"<td>{r['tier']}</td>"
        f"<td class='chg{' hot' if r['changed'] else ''}'>{'换仓' if r['changed'] else '—'}</td>"
        f"<td>{r['pos']:.0%}</td>"
        f"<td class='{'pos' if r['skret']>0 else 'neg' if r['skret']<0 else ''}'>"
        f"{r['skret']:+.2%}</td>"
        f"<td class='{'pos' if r['idxret']>0 else 'neg' if r['idxret']<0 else ''}'>"
        f"{r['idxret']:+.2%}</td>"
        f"<td>{r['nav']:.4f}</td></tr>" for r in rows)

    dates_json = [r["date"] for r in rows]
    nav_json = [f"{r['nav']:.4f}" for r in rows]
    idx_json = []
    base = closes[w_start + start]
    for k in range(len(rows)):
        i = k + w_start
        idx_json.append(f"{closes[i+start+1]/base-1:+.4f}")
    idx_json = ",".join(idx_json)

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>创业板择时 · 近二月回测详情</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;background:#f7f8fa;color:#1f2328}}
 .wrap{{max-width:1080px;margin:0 auto;padding:24px 20px 60px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#656d76;font-size:13px;margin-bottom:18px}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:18px 0}}
 .card{{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:14px 16px}}
 .card .v{{font-size:20px;font-weight:700;margin-top:4px}} .card .l{{color:#656d76;font-size:12px}}
 .card .v.pos{{color:#d1242f}} .card .v.neg{{color:#0a8f3c}}
 .card .v.small{{font-size:15px;font-weight:600}}
 #chart{{height:300px;margin:18px 0}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1e4e8;border-radius:10px;overflow:hidden;font-size:13px}}
 th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid #eef0f2}} th:first-child,td:first-child{{text-align:left}}
 th{{background:#f6f8fa;font-weight:600;position:sticky;top:0;font-size:12px;color:#57606a}}
 tr:hover{{background:#f6f8fa}} .pos{{color:#d1242f}} .neg{{color:#0a8f3c}}
 .chg.hot{{color:#9a6700;font-weight:700}}
 .note{{font-size:12px;color:#656d76;margin-top:14px;line-height:1.7}}
 footer{{font-size:12px;color:#8b949e;margin-top:24px;text-align:center}}
</style></head><body><div class="wrap">
<h1>创业板择时 · 近二月回测详情</h1>
<div class="sub">{d0} ~ {d1}（{len(rows)} 个交易日）· 核心层10因子五维·状态机 · 不注入估值(样本外负贡献关闭)</div>

<div class="cards">
 <div class="card"><div class="l">区间策略累计</div><div class="v {'pos' if seg_strat>0 else 'neg'}">{seg_strat:+.2%}</div></div>
 <div class="card"><div class="l">区间买入持有</div><div class="v {'pos' if seg_bh>0 else 'neg'}">{seg_bh:+.2%}</div></div>
 <div class="card"><div class="l">换仓次数</div><div class="v">{switches}</div></div>
 <div class="card"><div class="l">平均仓位</div><div class="v small">{avg_pos:.0%}</div></div>
 <div class="card"><div class="l">全区间核心层累计</div><div class="v small">{total_all:+.1%}</div></div>
</div>

<div id="chart"></div>
<table>
<thead><tr><th>日期</th><th>核心分</th><th>硬风控帽</th><th>档位</th><th>动作</th><th>实际仓位</th><th>策略当日</th><th>指数当日</th><th>净值</th></tr></thead>
<tbody>{hdr}</tbody></table>

<div class="note">
口径：信号日 d 仅用 ≤d-1 收盘（因子无前视），仓位吃 d+1 收益（对齐场外基金T+1）；
升档需连续2日确认、降档当日生效；成本0。<br>
档位线：&ge;+0.40满仓 / &ge;-0.15九成 / &ge;-0.30六成底仓 / 更低空仓。硬风控触即时覆盖。
</div>
<footer>generated by scripts/_backtest_2m.py · 仅参考，非投资建议</footer>
</div>
<script>
var cd=[{"," .join(['"'+r['date']+'"' for r in rows])}];
var cn=[{nav_json}];var ci=[{idx_json}];
var ch=echarts.init(document.getElementById('chart'));
ch.setOption({{tooltip:{{trigger:'axis'}},legend:{{data:['策略','买入持有']}},
 grid:{{left:60,right:20,top:30,bottom:40}},
 xAxis:{{type:'category',data:cd,axisLabel:{{fontSize:10}}}},
 yAxis:{{type:'value',axisLabel:{{formatter:function(v){{return v.toFixed(2)}}}}}},
 series:[
  {{name:'策略',type:'line',data:cn,showSymbol:false,lineStyle:{{color:'#d1242f',width:2}},
    areaStyle:{{opacity:0.06}}}},
  {{name:'买入持有',type:'line',data:ci,showSymbol:false,lineStyle:{{color:'#57606a',width:1.5,dash:[4,3]}}}}
 ]}});
</script></body></html>"""


if __name__ == "__main__":
    main()