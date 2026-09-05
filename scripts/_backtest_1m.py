# -*- coding: utf-8 -*-
"""近一月回测详情（_backtest_1m.py）—— 临时脚本,不入库
口径与官方 run_backtest 一致（load_index_sina + 核心层10因子五维 + 状态机 + 硬风控,不注入估值）。
方案 B 报表：把【决策日 d】与【收益日 d+1】彻底分离。
  - 决策日 d  ：信号日,核心分/档位/仓位/动作 都是 d 日（因子用 d 当日收盘）
  - 收益日 d+1：回报发生日,「策略当日」「指数当日」「净值」统一标 d+1
    （实盘 14:30 在 d 决策、15:00 下单,份额次日确认、吃 d→d+1 涨跌,对齐场外基金 T+1）
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy import chinext_timing as ct
from src.strategy import chinext_factors as cf
from src.strategy.data import load_index_sina

SYMBOL = "399006"
WIN = 22
FEE = 0.0


def main():
    df = load_index_sina(SYMBOL)
    if df is None or df.empty:
        raise SystemExit("399006 数据加载失败")
    closes = df["close"].tolist()
    amounts = (df["amount"].tolist() if "amount" in df else [0.0] * len(closes))
    date_strs = [d.strftime("%Y-%m-%d") for d in df.index]
    n = len(closes)
    start = 60

    signals = cf.core_signals(closes, amounts, erp_pctile=None)
    comp = cf.dimension_score(signals, {"趋势": 0.35, "量价": 0.20, "波动": 0.20,
                                        "估值": 0.10, "落袋": 0.15})

    # 主循环：决策日 d 用 ≤d 收盘算信号,吃 d->d+1 收益（无前视）
    prev = {"position": 0.0, "pending": None}
    nav, peak = 1.0, 1.0
    navs = []
    rows_raw = []
    for d in range(start, n - 1):
        caps = cf.defensive_state(closes[: d + 1], None,
                                  {"risk_off": False, "basis_min_ap": None,
                                   "intraday_pct": 0.0})
        dec = ct.decide_position(comp[d], caps["cap"], prev, tiers=ct.TIERS)
        if dec["changed"]:
            nav *= (1 - FEE * abs(dec["position"] - prev["position"]))
        prev = {"position": dec["position"], "pending": dec["pending"]}
        r = closes[d + 1] / closes[d] - 1.0          # d -> d+1 收益（策略T+1吃）
        nav *= (1 + dec["position"] * r)
        peak = max(peak, nav)
        navs.append(nav)
        idx_d = closes[d] / closes[d - 1] - 1.0       # 决策日 d 当天指数涨跌
        rows_raw.append({
            "dec_day": date_strs[d],
            "ret_day": date_strs[d + 1],
            "comp": comp[d],
            "cap": caps["cap"],
            "tier": _tier_text(dec["position"]),
            "changed": dec["changed"],
            "pos": dec["position"],
            "skret": dec["position"] * r,             # 策略 d->d+1 收益
            "idx_d": idx_d,                           # 指数 d 日当天涨跌
            "nav": nav,
        })

    total_all = navs[-1] - 1.0

    # 窗口 = 最后 WIN 个决策日
    w_start = len(rows_raw) - WIN
    seg = rows_raw[w_start:]
    d0 = seg[0]["dec_day"]
    d1 = seg[-1]["ret_day"]

    nav0 = navs[w_start - 1] if w_start > 0 else 1.0
    seg_strat = seg[-1]["nav"] / nav0 - 1.0
    bh0 = closes[w_start + start]
    bh1 = closes[len(rows_raw) - 1 + start + 1]     # 最后决策日的收益日收盘
    seg_bh = bh1 / bh0 - 1.0
    switches = sum(1 for r in seg if r["changed"])
    avg_pos = sum(r["pos"] for r in seg) / len(seg)

    # 曲线序列：策略净值（含 d->d+1 收益）；指数按决策日 d 当日涨跌累计
    cn = [f"{r['nav'] / nav0:.4f}" for r in seg]
    cum = 1.0
    ij = []
    for r in seg:
        cum *= (1 + r["idx_d"])
        ij.append(f"{cum - 1.0:.4f}")  # 决策日累计（相对首决策日）
    dates_json = ",".join(f"'{r['dec_day']}'" for r in seg)
    nav_json = ",".join(cn)
    idx_json = ",".join(ij)

    html = _render(d0, d1, seg_strat, seg_bh, switches, avg_pos, seg,
                   total_all, dates_json, nav_json, idx_json)
    out = PROJECT_ROOT / "docs" / "近一月回测详情.html"
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}")
    print(f"窗口 {d0} 决策 ~ {d1} 收益（{len(seg)} 决策日）：策略 {seg_strat:+.2%} / "
          f"持有 {seg_bh:+.2%} / 换仓 {switches} / 均仓 {avg_pos:.0%}")
    print(f"全区间(核心层)累计 {total_all:+.1%}")


def _tier_text(p):
    if p >= 0.999:
        return "满仓100%"
    if p > 0.85:
        return "九成90%"
    if p > 0.5:
        return "六成底仓"
    if p >= 0.001:
        return "三成仓位"
    return "空仓0%"


def _render(d0, d1, seg_strat, seg_bh, switches, avg_pos, seg, total_all,
            dates_json, nav_json, idx_json):
    hdr = "".join(
        f"<tr><td>{r['dec_day']}</td>"
        f"<td class='{'pos' if r['comp']>0 else 'neg' if r['comp']<0 else ''}'>{r['comp']:+.2f}</td>"
        f"<td>{r['cap']:.0%}</td><td>{r['tier']}</td>"
        f"<td class='chg{' hot' if r['changed'] else ''}'>{'换仓' if r['changed'] else '—'}</td>"
        f"<td>{r['pos']:.0%}</td>"
        f"<td><b>{r['ret_day']}</b></td>"
        f"<td class='{'pos' if r['skret']>0 else 'neg' if r['skret']<0 else ''}'>{r['skret']:+.2%}</td>"
        f"<td class='{'pos' if r['idx_d']>0 else 'neg' if r['idx_d']<0 else ''}'>{r['idx_d']:+.2%}</td>"
        f"<td>{r['nav']:.4f}</td></tr>" for r in seg)

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>创业板择时 · 近一月回测详情</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;background:#f7f8fa;color:#1f2328}}
 .wrap{{max-width:1120px;margin:0 auto;padding:24px 20px 60px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#656d76;font-size:13px;margin-bottom:18px}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:18px 0}}
 .card{{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:14px 16px}}
 .card .d{{color:#656d76;font-size:12px}} .card .v{{font-size:19px;font-weight:700;margin-top:4px}}
 .card .v.small{{font-size:14px}} .card .v.pos{{color:#d1242f}} .card .v.neg{{color:#0a8f3c}}
 #chart{{height:300px;margin:18px 0}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1e4e8;border-radius:10px;overflow:hidden;font-size:13px}}
 th,td{{padding:8px 9px;text-align:right;border-bottom:1px solid #eef0f2}}
 th:first-child,td:first-child{{text-align:left}}
 th{{background:#f6f8fa;font-weight:600;position:sticky;top:0;font-size:12px;color:#57606a}}
 .grp{{background:#eef3ff;color:#3f5ef0;font-weight:700;letter-spacing:.5px}}
 tr:hover{{background:#f6f8fa}} .pos{{color:#d1242f}} .neg{{color:#0a8f3c}}
 .chg.hot{{color:#9a6700;font-weight:700}}
 .note{{font-size:12px;color:#656d76;margin-top:14px;line-height:1.8}}
 footer{{font-size:12px;color:#8b949e;margin-top:24px;text-align:center}}
</style></head><body><div class="wrap">
<h1>创业板择时 · 近一月回测详情</h1>
<div class="sub">{d0} 决策 ~ {d1} 收益（{len(seg)} 个决策日）· 核心层10因子五维+状态机+硬风控 · 不注入估值(样本外负贡献关闭)</div>

<div class="cards">
 <div class="card"><div class="d">区间策略累计</div><div class="v {'pos' if seg_strat>0 else 'neg'}">{seg_strat:+.2%}</div></div>
 <div class="card"><div class="d">区间买入持有</div><div class="v {'pos' if seg_bh>0 else 'neg'}">{seg_bh:+.2%}</div></div>
 <div class="card"><div class="d">换仓次数</div><div class="v">{switches}</div></div>
 <div class="card"><div class="d">平均仓位</div><div class="v small">{avg_pos:.0%}</div></div>
 <div class="card"><div class="d">全区间核心层累计</div><div class="v small">{total_all:+.1%}</div></div>
</div>

<div id="chart"></div>
<table>
<thead><tr>
 <th colspan="6"><span class="grp">决策日 d（信号:核心分用 d 收盘·指数 d 日涨跌）</span></th>
 <th colspan="4"><span class="grp">收益日 d+1（策略T+1回报）</span></th>
</tr><tr>
 <th>决策日</th><th>核心分d</th><th>硬风控帽</th><th>档位</th><th>动作</th><th>实仓</th>
 <th>收益日</th><th>策略d+1</th><th>指数d日</th><th>净值(d+1)</th>
</tr></thead>
<tbody>{hdr}</tbody></table>

<div class="note">
<strong>口径说明</strong>：信号日在 d,因子只用 ≤d 收盘（无前视,决策日 d 含当日收盘）；仓位吃 d→d+1 收益。
「决策日」栏列 d（信号/档位/仓位/动作/指数d日）；「收益日」栏列 d+1（策略回报/净值）。<br>
<strong>指数d日</strong> = 决策日 d 当天指数涨跌（用于对照当日盘面）；<strong>策略d+1</strong> = 决策后 T+1 吃到的收益（满仓时策略d+1≈指数d+1,即次一交易日振幅）。<br>
实盘 14:30 于 d 决策、15:00 前下单,场外基金份额次日确认、吃 d→d+1 涨跌,与回测 T+1 对齐。<br>
升档需连续2日确认、降档当日生效；成本0。档位线：≥+0.40满仓 / ≥-0.15九成 / ≥-0.30六成底仓 / 更低空仓；硬风控触即时覆盖。<br>
例：决策日 6-22（核心分+0.46、满仓,指数d日 +2.25%）→ 策略T+1吃 6-23 指数 -3.84%（当日满仓≈指数收益 -3.84%）。
</div>
<footer>generated by scripts/_backtest_1m.py · 仅参考，非投资建议</footer>
</div>
<script>
var mdates=[{dates_json}];var cn=[{nav_json}];var ci=[{idx_json}];
var ch=echarts.init(document.getElementById('chart'));
ch.setOption({{tooltip:{{trigger:'axis'}},legend:{{data:['策略净值','指数累计']}},
 grid:{{left:60,right:20,top:30,bottom:40}},
 xAxis:{{type:'category',data:mdates,axisLabel:{{fontSize:10}}}},
 yAxis:{{type:'value',axisLabel:{{formatter:function(v){{return (v*100).toFixed(0)+'%'}}}}}},
 series:[
  {{name:'策略净值',type:'line',data:cn,showSymbol:false,lineStyle:{{color:'#d1242f',width:2}},areaStyle:{{opacity:0.06}}}},
  {{name:'指数累计',type:'line',data:ci,showSymbol:false,lineStyle:{{color:'#57606a',width:1.5,type:'dashed'}}}}
 ]}});
</script></body></html>"""


if __name__ == "__main__":
    main()