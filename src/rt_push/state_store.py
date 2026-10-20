# filepath: src/rt_push/state_store.py
"""状态管理：seen/pending 裁剪、Gist 状态合并、事件档案（自 scripts/real_time_push.py 拆出，行为不变）"""

import json
import logging
import os

from src.rt_push.common import _lcs_len


logger = logging.getLogger("real_time_push")



# 未推条目的独立窗口（小时，2026-09-01 新增）：见 _expire_seen_fingerprints 说明。
# 已推条目仍需完整 48h（复盘列表 + 48h 同事件拦截依赖）。
STATE_WINDOW_HOURS_UNPUSHED = 24


# 回测事件档案上限（2026-09-08 P0-2 新增）：pushed_events 只留 48h，被淘汰条目
# 转存 backtest_events 供 signal_backtest 统计后 1/3/5/10 日收益（此前 48h 外事件
# 永久丢失，后 3/5/10 日维度永远无数据）。2000 条 × ~300B ≈ 600KB 已在
# Gist 体积守卫（_gist_save）的 backtest_events 裁剪兜底范围内。
BACKTEST_EVENTS_MAX = 2000


# 候选溢出挂起重试上限（2026-08-06 新增）：同一指纹连续 N 轮溢出后放弃（写 seen），
# 防止突发行情持续超限时 pending 无限累积 / 无限重试消耗 LLM 额度
MAX_PENDING_RETRY = 3
# 高信号条目单独保留更长但有限的重试窗口；超过后同样落 seen，防止占满 pending。
MAX_PENDING_RETRY_HIGH_SIGNAL = 10


def _pending_retry_limit(item: dict) -> int:
    """返回候选溢出条目的有限重试上限。"""
    return MAX_PENDING_RETRY_HIGH_SIGNAL if item.get("_hit_signal") else MAX_PENDING_RETRY


# pending 序列化字节上限（2026-09-04 P1-1 新增）：挂起条目带全量 payload 后
# 体积上升，200 条 × ~700B ≈ 140KB，叠加 seen(700KB)+pushed/candidate(210KB)
# 需守住 Gist 单文件 1MB 硬限。超限按时间裁剪到 150 条。
PENDING_MAX_BYTES = 200_000


# seen 指纹条数上限（2026-08-13 P0 新增）：48h 清理后仍超上限则按时间保留最新。
# 此前 seen 无上限（峰值 4759 条 → 状态文件 0.72MB），Gist 写入截断损坏是
# "状态被覆盖清空"事故的诱因。
# 2026-09-01 上调 3000 → 12000：实测单日未推条目（pushed=False）达 2972 条，
# 3000 上限一天就被击穿，seen 时间窗口缩到不足 1 天（09-01 04:03~22:33），
# 凌晨已推事件被挤出 seen → 盘后复盘漏条 + 次日重复推送风险。
# 体积按 _prune_seen 的字节上限兜底：Gist 单文件 1MB 硬限（8-13 状态文件
# 膨胀到 0.72MB 时即发生过写入截断损坏），700KB 留足余量。条数上限只是
# 第一道闸，真正决定能不能安全落盘的是字节上限。
SEEN_MAX = 12000
SEEN_MAX_BYTES = 700_000


def _est_seen_bytes(seen: dict, sample: int = 200) -> int:
    """seen 序列化后的精确字节数（全量 json.dumps，与 _gist_save 写入口径一致）。

    2026-09-02 修正：原抽样估算口径（指纹 + t + title + 60B ≈ 150B/条）与
    实际序列化（全部字段 + indent=2 格式化 ≈ 175B/条）不一致，且记录里存
    的是 title_norm 而 title 字段往往不存在——估算系统性偏低。9-02 实证：
    4055 条估算 607KB 未触发 700KB 裁剪，实际 709KB。700KB 级整表 dumps
    每轮一次成本可忽略，改精确计算后 SEEN_MAX_BYTES 才是真硬上限。
    注意：该上限只约束 seen 本身；整文件还含 candidate_events/pending/
    pushed_events（约 +210KB），917KB 实证写入侧仍安全（<1MB Gist 硬限），
    读取侧已由 _gist_load 的 raw_url 回退根治，不再依赖体积控制。
    """
    if not seen:
        return 0
    return len(json.dumps(seen, ensure_ascii=False, indent=2).encode("utf-8"))


def _est_pending_bytes(pending: dict) -> int:
    """pending 序列化后的精确字节数（2026-09-04 P1-1：带 payload 后体积兜底用）"""
    if not pending:
        return 0
    return len(json.dumps(pending, ensure_ascii=False, indent=2).encode("utf-8"))


def _prune_seen(seen: dict, max_items: int = SEEN_MAX,
                max_bytes: int = SEEN_MAX_BYTES) -> dict:
    """指纹表裁剪：条数与字节双上限，超限时优先淘汰未推条目。

    2026-09-01：pushed=True 的条目是盘后复盘列表与 48h 同事件拦截的依据，
    pushed=False 只是"本轮已处理、别再判"的标记，去重价值低。单纯按时间
    裁剪会把当天已推事件挤出去——9-01 实锤：00:32 推送的「君正股份 DRAM
    涨价」被挤出 seen，复盘"今日已推事件"直接漏掉这一条。
    排序键 (pushed, t) 使 False 组整体排在前面，组内按时间升序，
    于是切片淘汰的是"最老的未推条目"。
    字节上限优先于条数上限：Gist 单文件 1MB 是硬限，超限会写入截断损坏
    （8-13 事故根因），条数再多也必须让位于体积安全。
    """
    if not seen:
        return seen
    per = max(_est_seen_bytes(seen) / len(seen), 1.0)
    allowed = min(max_items, max(1, int(max_bytes / per)))
    if len(seen) <= allowed:
        return seen
    items = sorted(seen.items(),
                   key=lambda kv: (bool(kv[1].get("pushed")), str(kv[1].get("t", ""))))
    return dict(items[len(items) - allowed:])


GIST_STATE_SEEN_KEEP = 1800


def _compress_seen_for_gist(seen: dict, keep: int = GIST_STATE_SEEN_KEEP) -> dict:
    """体积守卫压缩：按时间保留最近 keep 条指纹记录（体积优先于去重覆盖面）。"""
    if len(seen) <= keep:
        return seen
    items = sorted(seen.items(), key=lambda kv: str(kv[1].get("t", "")))
    return dict(items[len(items) - keep:])


# ============================================================
# 状态持久化（Gist 云端 / 本地文件）
# ============================================================
def _empty_state() -> dict:
    return {
        "version": 2,
        "seen": {},  # {fingerprint: {"t": "YYYY-MM-DD HH:MM:SS", "pushed": bool, "title": str}}
        # 候选溢出挂起重试（2026-08-06 新增，防漏推）：
        # 单轮候选超上限被溢出的条目不写 seen（否则 48h 内永久放弃 → 漏推），
        # 而是记入 pending 并在下轮重新进入 LLM 判定。
        # {fingerprint: {"t": str, "retry": int, "title": str}}
        "pending": {},
        # 已推送事件签名（推送级同事件去重，48h 窗口）：
        # [{"t": str, "stocks": [], "entities": [], "events": [], "numbers": [], "title_norm": str}]
        "pushed_events": [],
        # 当日预筛候选（P7-1 2026-08-22 新增）：经 LLM 判定的全部重大候选（含方向+事件签名），
        # 供创业板择时 news_modifier 消费"已推送 ∪ 预筛候选"，覆盖全部重大候选而非仅强档推送。
        # 记录于 LLM 判定后的每一候选（无论最终推/不推）；pushed_events 仍为"实际已推送"权威。
        # [{**事件签名, "dir": str, "t": str, "pushed": bool}]
        "candidate_events": [],
        # watchlist 公告直通限频日志（2026-09-07 P1 新增）：
        # [{"key": "code|类别", "t": str}]，同股同类别 24h 限推 1 条（防刷屏）。
        "watch_announce": [],
        # 回测事件档案（2026-09-08 P0-2 新增）：pushed_events 48h 清理时被淘汰的
        # 条目原样转存于此，供 signal_backtest 统计后 1/3/5/10 日收益。
        # 条目结构与 pushed_events 完全一致。
        "backtest_events": [],
    }


def _is_ci() -> bool:
    """GitHub Actions 环境检测（CI=true 由 GitHub 自动注入）"""
    return os.getenv("CI", "").strip().lower() == "true"


def _pending_same_as_pushed(pend_title: str, pushed_title: str) -> bool:
    """pending 清理用宽松同事件判定（归一化标题字符集 Jaccard 或 LCS 覆盖足够高）"""
    if not pend_title or not pushed_title:
        return False
    ja = len(set(pend_title) & set(pushed_title)) / len(set(pend_title) | set(pushed_title))
    if ja >= 0.55:
        return True
    shorter = min(len(pend_title), len(pushed_title))
    return shorter >= 6 and _lcs_len(pend_title, pushed_title) / shorter >= 0.5


def _pend_payload(n: dict) -> dict:
    """pending 挂起条目的全量 payload（2026-09-04 P1-1 主动重注入用）。

    溢出条目滚出源窗口（如财联社 20 条滚动窗）后，此前只能等重新被抓取
    （fp 相同）才重回判定，否则永久躺 pending 到 48h 过期。存全量 payload
    供下轮主动重注入。只保留预筛/LLM 判定所需字段，截断控 Gist 体积
    （中文 UTF-8 每字符 3 字节，200 条 × ~700B ≈ 140KB）。"""
    return {
        "title": str(n.get("title", "") or "")[:80],
        "content": str(n.get("content", "") or "")[:120],
        "source": str(n.get("source", "") or "")[:30],
        "published_at": str(n.get("published_at", "") or "")[:30],
    }


def _event_sig_key(e: dict) -> str:
    """事件签名内容键（状态合并去重用）"""
    return ("|".join(sorted(e.get("entities") or [])) + "#"
            + "|".join(sorted(e.get("events") or [])) + "#"
            + "|".join(sorted(e.get("numbers") or [])) + "#"
            + (e.get("title_norm") or ""))


def _day_key(e: dict) -> str:
    """事件所在日期（YYYY-MM-DD，取 t 字段前缀）"""
    return str(e.get("t") or "")[:10]


def _mark_candidate_pushed(state: dict, ck: tuple) -> None:
    """推送成功后回填 candidate_events 的 pushed 标记（2026-09-07 P0 审计 1.4B）。

    此前 pushed 字段写死 False 从无置 True 路径。按 (日期, 事件签名) 键回查
    （勿持列表下标——_merge_state 重新去重后引用/顺序会变），命中即置 True。
    """
    for ce in state.get("candidate_events") or []:
        if (_day_key(ce), _event_sig_key(ce)) == ck:
            ce["pushed"] = True


def _merge_state(local: dict, remote: dict) -> dict:
    """合并两份状态（Gist 读-改-写防并发覆盖）：取并集，pushed=True 优先

    本地 --loop 与云端 GitHub Actions 共享同一 Gist 时，两方各自读-改-写，
    直接覆盖写入会丢掉另一方新增的 pushed 标记 → 同一条被重复推送（2026-08-01 实证：
    云端已推送记录被本地轮询覆盖后，Gist 中 pushed=true 记录消失）。
    合并后冲突窗口从"整轮执行"缩小到"单次写入"。
    """
    merged_seen = dict(remote.get("seen", {}) or {})
    for fp, rec in (local.get("seen", {}) or {}).items():
        old = merged_seen.get(fp)
        if old is None or (rec.get("pushed") and not old.get("pushed")):
            merged_seen[fp] = rec
    local["seen"] = merged_seen

    # 合并挂起重试（pending）：retry 计数取较大者（防并发覆盖丢计数）
    merged_pending = dict(remote.get("pending", {}) or {})
    for fp, rec in (local.get("pending", {}) or {}).items():
        old = merged_pending.get(fp)
        if old is None:
            merged_pending[fp] = rec
        else:
            merged_pending[fp] = rec if int(rec.get("retry", 0)) >= int(old.get("retry", 0)) else old
    local["pending"] = merged_pending

    merged_events = {_event_sig_key(e): e for e in (remote.get("pushed_events") or [])}
    for e in (local.get("pushed_events") or []):
        merged_events.setdefault(_event_sig_key(e), e)
    local["pushed_events"] = list(merged_events.values())

    # 合并回测事件档案（2026-09-08 P0-2）：与 pushed_events 同口径按 _event_sig_key
    # 取并集（同 key 远端优先），防本地与云端并发写时档案互冲丢失。
    merged_backtest = {_event_sig_key(e): e for e in (remote.get("backtest_events") or [])
                       if isinstance(e, dict)}
    for e in (local.get("backtest_events") or []):
        if isinstance(e, dict):
            merged_backtest.setdefault(_event_sig_key(e), e)
    local["backtest_events"] = list(merged_backtest.values())

    # 合并当日预筛候选（P7-1）：按 (日期, 事件签名) 去重，pushed=True 优先（并发写防丢方向）。
    # 并发实例各自读-改-写时，直接覆盖会把另一方新增的候选方向丢掉 → 合并后冲突窗口收窄到单次写入。
    merged_cands = {}
    for e in (remote.get("candidate_events") or []):
        merged_cands[(_day_key(e), _event_sig_key(e))] = e
    for e in (local.get("candidate_events") or []):
        _k = (_day_key(e), _event_sig_key(e))
        old = merged_cands.get(_k)
        if old is None or (e.get("pushed") and not old.get("pushed")):
            merged_cands[_k] = e
    local["candidate_events"] = list(merged_cands.values())

    # 合并 watchlist 公告直通限频日志（2026-09-07 P1）：按 (key, t) 去重并集
    merged_wa = {(e.get("key"), e.get("t")): e
                 for e in (remote.get("watch_announce") or []) if isinstance(e, dict)}
    for e in (local.get("watch_announce") or []):
        if isinstance(e, dict):
            merged_wa.setdefault((e.get("key"), e.get("t")), e)
    local["watch_announce"] = list(merged_wa.values())
    return local
