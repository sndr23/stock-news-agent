"""离线重放推送质量审计快照，比较 Q-01/Q-03 修复前后的结果。

用法：
    python scripts/_replay_audit_fix.py
    python scripts/_replay_audit_fix.py --state C:/path/rt_state.json

脚本只读取快照和生产模块，不调用数据源、LLM 或推送通道。
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path(
    r"C:\Users\mxs\AppData\Local\hermes\cache\scratch\rt_state.json"
)


def _load_runtime_module():
    path = ROOT / "scripts" / "real_time_push.py"
    spec = importlib.util.spec_from_file_location("real_time_push_replay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载生产入口: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_state(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        raise ValueError("快照根节点必须是 JSON 对象")
    return state


def _event_title(event: dict) -> str:
    return str(event.get("title") or event.get("title_norm") or "").strip()


def _event_label(index: int, event: dict) -> str:
    title = _event_title(event)
    return f"#{index} {event.get('t', '')} {title[:100]}"


def _pair_matrix(records: list[dict], same_event) -> set[tuple[int, int]]:
    return {
        (left, right)
        for left, right in combinations(range(len(records)), 2)
        if same_event(records[left], records[right])
    }


def _old_pairs(runtime, records: list[dict]) -> set[tuple[int, int]]:
    """调用 Q-01 前规则：去掉新增核心事实判定和两个长鑫别名。"""
    original_alias = runtime._ENTITY_ALIAS
    original_core_facts = runtime._same_event_core_facts
    old_alias = dict(original_alias)
    old_alias.pop("长鑫存储", None)
    old_alias.pop("CXMT", None)
    runtime._ENTITY_ALIAS = old_alias
    runtime._same_event_core_facts = lambda _ctx: False
    try:
        return _pair_matrix(records, runtime._is_same_event)
    finally:
        runtime._ENTITY_ALIAS = original_alias
        runtime._same_event_core_facts = original_core_facts


def _components(records: list[dict], pairs: set[tuple[int, int]]) -> list[tuple[int, ...]]:
    parent = list(range(len(records)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left, right in pairs:
        union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(records)):
        groups.setdefault(find(index), []).append(index)
    return sorted(
        (tuple(members) for members in groups.values() if len(members) > 1),
        key=lambda members: members[0],
    )


def _print_duplicate_changes(runtime, pushed: list[dict]) -> None:
    old_pairs = _old_pairs(runtime, pushed)
    new_pairs = _pair_matrix(pushed, runtime._is_same_event)
    added_pairs = sorted(new_pairs - old_pairs)
    removed_pairs = sorted(old_pairs - new_pairs)

    print("\n[Q-01] 重复组变化")
    print(f"旧规则重复配对: {len(old_pairs)}；新规则重复配对: {len(new_pairs)}")
    print(f"新增合并配对: {len(added_pairs)}；不再合并配对: {len(removed_pairs)}")
    for left, right in added_pairs:
        print(f"  + {_event_label(left, pushed[left])}")
        print(f"    {_event_label(right, pushed[right])}")
    for left, right in removed_pairs:
        print(f"  - {_event_label(left, pushed[left])}")
        print(f"    {_event_label(right, pushed[right])}")

    changed_pairs = set(added_pairs) | set(removed_pairs)
    old_groups = _components(pushed, old_pairs)
    new_groups = _components(pushed, new_pairs)
    old_changed = [
        group for group in old_groups
        if any((min(left, right), max(left, right)) in changed_pairs
               for left, right in combinations(group, 2))
    ]
    new_changed = [
        group for group in new_groups
        if any((min(left, right), max(left, right)) in changed_pairs
               for left, right in combinations(group, 2))
    ]
    if old_changed or new_changed:
        print("受影响的连通重复组:")
        for label, groups in (("旧", old_changed), ("新", new_changed)):
            for group in groups:
                print(f"  {label}: {[index for index in group]}")
    else:
        print("受影响的连通重复组: 无")


def _snapshot_clock(runtime, state: dict, pushed: list[dict], candidates: list[dict]) -> float:
    timestamps = []
    for event in [*pushed, *candidates]:
        timestamp = runtime._state_timestamp(str(event.get("t") or ""))
        if timestamp is not None:
            timestamps.append(timestamp)
    if not timestamps:
        raise ValueError("快照没有可解析的事件时间")
    return max(timestamps)


def _candidate_news(candidate: dict) -> dict:
    return {
        "title": _event_title(candidate),
        "content": str(candidate.get("content") or ""),
    }


def _seen_saturation_match(candidate: dict, seen: dict) -> bool:
    title = _event_title(candidate)
    compact_title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", title)[:12]
    if not compact_title:
        return False
    return any(
        "[同题材已饱和]" in str(record.get("title") or "")
        and compact_title in re.sub(r"\s+", "", str(record.get("title") or ""))
        for record in seen.values()
        if isinstance(record, dict)
    )


def _replay_topic_exemptions(runtime, state: dict, pushed: list[dict],
                             candidates: list[dict]) -> None:
    """按候选时间顺序模拟 Q-03，一条事件链最多消费一次豁免。"""
    replay_pushed = copy.deepcopy(pushed)
    newly_allowed = []
    invalid_exemptions = []
    duplicate_exemptions = []
    old_saturation_hits = []
    chain_counts: Counter[str] = Counter()

    for candidate in sorted(candidates, key=lambda item: str(item.get("t") or "")):
        if candidate.get("pushed"):
            continue
        news = _candidate_news(candidate)
        judge = {"direction": candidate.get("dir")}
        if not runtime._topic_saturated(candidate, replay_pushed):
            continue

        facts = runtime._hard_event_novelty_facts(news)
        chain = runtime._hard_event_chain_key(candidate, news)
        old_saturation_hits.append(candidate)
        available = runtime._hard_event_topic_exemption_available(
            news, judge, candidate, replay_pushed
        )
        duplicate = any(runtime._is_same_event(candidate, pushed_event)
                         for pushed_event in replay_pushed)
        if not available or duplicate:
            if available and duplicate:
                duplicate_exemptions.append(candidate)
            continue

        newly_allowed.append((candidate, facts, chain))
        chain_counts[chain] += 1
        if (judge.get("direction") not in ("bullish", "bearish")
                or not facts or not chain or duplicate):
            invalid_exemptions.append(candidate)

        replay_pushed.append({
            **candidate,
            "topic_exemption": True,
            "topic_exemption_chain": chain,
        })

    repeated_chains = {chain: count for chain, count in chain_counts.items()
                       if count > 1}
    stale_labels = [
        candidate for candidate, _facts, _chain in newly_allowed
        if _seen_saturation_match(candidate, state.get("seen") or {})
    ]

    print("\n[Q-03] 新增放行条目")
    print(f"快照中命中旧饱和规则的候选: {len(old_saturation_hits)}")
    print(f"新规则新增放行: {len(newly_allowed)}")
    for candidate, facts, chain in newly_allowed:
        print(f"  + {candidate.get('t', '')} {_event_title(candidate)[:100]}")
        print(f"    direction={candidate.get('dir')} facts={sorted(facts)} chain={chain}")
    if not newly_allowed:
        print("  （无）")

    print("\n[Q-03] 误推检查")
    print(f"非 bullish/bearish 或无硬事实的豁免: {len(invalid_exemptions)}")
    print(f"同一事件链重复豁免: {len(repeated_chains)}")
    print(f"命中豁免但与既有推送重复（应拦截）: {len(duplicate_exemptions)}")
    print(f"历史上被[同题材已饱和]拦截、现在可放行: {len(stale_labels)}")
    status = "PASS" if not invalid_exemptions and not repeated_chains else "FAIL"
    print(f"误推检查结论: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线重放推送质量 P1 修复")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE,
                        help="rt_state.json 快照路径")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    state = _load_state(args.state)
    runtime = _load_runtime_module()
    pushed = list(state.get("pushed_events") or [])
    candidates = list(state.get("candidate_events") or [])

    print(f"快照: {args.state}")
    print(f"pushed_events={len(pushed)} candidate_events={len(candidates)} "
          f"pending={len(state.get('pending') or {})}")

    _print_duplicate_changes(runtime, pushed)

    # 让主题窗口相对于快照最后一条候选计算，避免脚本运行时刻改变回放结论。
    replay_now = _snapshot_clock(runtime, state, pushed, candidates)
    real_time = runtime.time.time
    runtime.time.time = lambda: replay_now
    try:
        _replay_topic_exemptions(runtime, state, pushed, candidates)
    finally:
        runtime.time.time = real_time
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
