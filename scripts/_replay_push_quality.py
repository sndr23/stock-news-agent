"""Replay push-quality rules against a real real_time_state.json snapshot.

Read-only and repeatable. The script evaluates the current rules, not the LLM
judgements recorded in the snapshot, because the snapshot has no judge payload.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BASE_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "stock-news-agent-p0-base"
DEFAULT_STATE = Path(
    r"C:/Users/mxs/AppData/Local/hermes/cache/scratch/news_audit/rt_state.json"
)


def _load_rtp():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "replay_real_time_push", ROOT / "scripts" / "real_time_push.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_watchlist() -> set[str]:
    data = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    return {str(item.get("name") or "") for item in data.get("stocks") or []}


def _news(event: dict) -> dict:
    return {
        "title": str(event.get("title") or event.get("title_norm") or ""),
        "content": "",
        "source": str(event.get("source") or ""),
    }


def _anchor_check(rtp, watchlist: set[str], titles: list[str]) -> list[tuple]:
    judge = {
        "push": True, "score": 8, "direction": "bullish",
        "scope": "sector", "is_leader_stock": False,
    }
    result = []
    for title in titles:
        reason = rtp._is_noise_push({"title": title}, judge, watchlist)
        result.append((title, reason or "KEEP"))
    return result


def _old_rules(path: Path):
    spec = importlib.util.spec_from_file_location(
        "replay_real_time_push_base", path / "scripts" / "real_time_push.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _count_old_rules(module, events: list[dict], watchlist: set[str]) -> Counter:
    counts = Counter()
    judge = {
        "push": True, "score": 8, "direction": "bullish",
        "scope": "market", "is_leader_stock": False,
    }
    for event in events:
        title = str(event.get("title") or event.get("title_norm") or "")
        reason = module._is_noise_push({"title": title}, judge, watchlist)
        if reason:
            counts[reason] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--baseline-root", type=Path, default=BASE_ROOT)
    parser.add_argument("--with-baseline", action="store_true")
    args = parser.parse_args()

    rtp = _load_rtp()
    watchlist = _load_watchlist()
    state = json.loads(args.state.read_text(encoding="utf-8"))
    events = [
        e for e in state.get("backtest_events") or [] if isinstance(e, dict)
    ]
    candidate_titles = {
        str(e.get("title_norm") or "")
        for e in state.get("candidate_events") or []
        if isinstance(e, dict)
    }
    pushed_titles = {
        str(e.get("title_norm") or "")
        for e in state.get("pushed_events") or []
        if isinstance(e, dict)
    }
    overlap = candidate_titles & pushed_titles
    baseline_counts = Counter()
    old = None
    if args.with_baseline and args.baseline_root and args.baseline_root.exists():
        old = _old_rules(args.baseline_root)
    if old:
        baseline_counts = _count_old_rules(old, events, watchlist)
    judge = {
        "push": True, "score": 8, "direction": "bullish",
        "scope": "sector", "is_leader_stock": False,
    }
    hits = []
    baseline_broadcast = 0
    baseline_noise = 0
    broadcast_blocked = 0
    watch_hits = 0
    watch_blocked = 0
    false_kills = []
    broad_candidates = []
    for event in events:
        news = _news(event)
        title = news["title"]
        reason = rtp._is_noise_push(news, judge, watchlist)
        is_broadcast = rtp._is_broadcast_body(title, exempt_names=set()) == "播报体"
        is_watch = any(name in title for name in watchlist)
        baseline_broadcast += int(is_broadcast)
        baseline_reason = rtp._is_noise_push(news, judge, set())
        baseline_noise += int(baseline_reason != "" or is_broadcast)
        watch_hits += int(is_watch)
        if reason:
            hits.append((reason, title))
            watch_blocked += int(is_watch)
            has_verb = any(v in title for v in rtp._BROADCAST_EVENT_VERBS)
            if is_watch or has_verb:
                false_kills.append((reason, title, is_watch, has_verb))
            if has_verb:
                broad_candidates.append((reason, title))

    counts = Counter(reason for reason, _ in hits)
    broadcast_blocked = sum(
        1 for _reason, title in hits
        if rtp._is_broadcast_body(title, exempt_names=set()) == "播报体"
    )
    print("PUSH_QUALITY_REPLAY")
    print(f"state={args.state}")
    print(f"total={len(events)}")
    print(f"candidate_events={len(candidate_titles)}")
    print(f"pushed_events={len(pushed_titles)}")
    print(f"historical_candidate_pushed_overlap={len(overlap)}")
    overlap_rows = [
        (str(e.get("title_norm") or ""), str(e.get("dir") or ""), str(e.get("t") or ""))
        for e in state.get("candidate_events") or []
        if isinstance(e, dict) and str(e.get("title_norm") or "") in overlap
    ]
    for title_norm, direction, timestamp in overlap_rows:
        print(
            "historical_overlap_detail="
            f"{title_norm}|dir={direction}|t={timestamp}|pushed="
            f"{next((x.get('pushed') for x in state.get('candidate_events') or [] if isinstance(x, dict) and x.get('title_norm') == title_norm), None)}"
        )
    if old:
        print(f"baseline_rule_hits={sum(baseline_counts.values())}")
        for reason in sorted(baseline_counts):
            print(f"baseline_rule.{reason}={baseline_counts[reason]}")
        baseline_column = 0
        baseline_intraday = 0
        for event in events:
            title = str(event.get("title") or event.get("title_norm") or "")
            if any(m in title for m in old._NOISE_COLUMN_MARKERS):
                rest = title
                for marker in old._NOISE_COLUMN_MARKERS:
                    rest = rest.replace(marker, "")
                if not (old._is_macro_data_release(rest)
                        or old._is_major_deal_event(rest)):
                    baseline_column += int(
                        old.has_signal_keyword(rest)
                        or old._is_index_move_text(rest)
                        or old._is_intraday_move_text(rest)
                    )
            elif (not any(k in title for k in old._MAJOR_CORPORATE_EVENT_MARKERS)
                  and (old._is_index_move_text(title)
                       or old._is_intraday_move_text(title))):
                baseline_intraday += 1
        print(f"baseline_static_column={baseline_column}")
        print(f"baseline_static_intraday={baseline_intraday}")
        print(f"baseline_static_total={baseline_column + baseline_intraday}")
    print(f"baseline_noise={baseline_noise}")
    print(f"new_noise={len(hits)}")
    print(f"pure_broadcast={baseline_broadcast}")
    print(f"broadcast_blocked={broadcast_blocked}")
    print(
        "broadcast_rate="
        f"{(broadcast_blocked / baseline_broadcast * 100 if baseline_broadcast else 0):.1f}%"
    )
    print(f"watchlist_titles={watch_hits}")
    print(f"watchlist_blocked={watch_blocked}")
    print(
        "watchlist_keep_rate="
        f"{((watch_hits - watch_blocked) / watch_hits * 100 if watch_hits else 0):.1f}%"
    )
    print(f"false_kill_candidates={len(false_kills)}")
    print(f"broadcast_verb_candidates={len(broad_candidates)}")
    for reason in sorted(counts):
        print(f"rule.{reason}={counts[reason]}")

    print("\nBLOCKED_TOP30")
    for index, (reason, title) in enumerate(hits[:30], 1):
        print(f"{index:02d}\t{reason}\t{title}")

    print("\nFALSE_KILL_CHECK")
    if not false_kills:
        print("NONE")
    else:
        explanations = {
            "两场重要发布会，明日举行；A股龙头，即将大手笔分红丨周末要闻速递":
                "日报栏目+展望性分红，无已发生重大事件",
            "【近一周融资资金呈现净偿还态势 融资资金出逃CPO龙头、存储...":
                "非日报栏目，旧盘面异动规则以 watchlist 为例外保留",
            "10月20日投资避雷针：高盛数据显示 对冲基金以至少十年来最快速度减持科技股":
                "投资避雷针为日报栏目，研报观点无公司公告事实",
        }
        for reason, title, is_watch, has_verb in false_kills:
            note = explanations.get(title, "")
            print(f"{reason}\twatch={is_watch}\tverb={has_verb}\t{title}\t{note}")

    print("\nBROADCAST_EVENT_VERB_CANDIDATES")
    if not broad_candidates:
        print("NONE")
    else:
        for reason, title in broad_candidates:
            print(f"{reason}\t{title}")

    print("\nANCHORS")
    blocked = [
        "物理AI概念局部回暖 天娱数科直线涨停",
        "人形机器人概念反复走强 兆威机电5天3板",
        "南方两倍做多海力士大涨19%",
        "CPO概念震荡走弱 东山精密跌停",
    ]
    kept = [
        "天孚通信:关于收到控股股东提议回购公司股份的提示性公告",
        "生益科技:生益科技关于以集中竞价交易方式回购股份方案的公告",
        "中际旭创：预计1.6T、800G等高端光模块需求将保持快速增长",
        "长鑫科技：预计2026年前三季度净利润1310亿元至1325亿元，同比扭亏为盈",
        "假期要闻汇总：非农数据意外“爆冷” 美联储10月加息概率骤降",
    ]
    for title, reason in _anchor_check(rtp, watchlist, blocked + kept):
        print(f"{reason}\t{title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
