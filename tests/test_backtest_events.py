# filepath: tests/test_backtest_events.py
"""回测事件档案（backtest_events，2026-09-08 P0-2）单元测试

背景：pushed_events 只留 48h（STATE_WINDOW_HOURS=48），被淘汰事件此前
永久丢失 → signal_backtest 统计后 1/3/5/10 日收益时后 3/5/10 日维度
永远无数据。修复：48h 清理时被淘汰条目原样转存 backtest_events 档案
（上限 BACKTEST_EVENTS_MAX=2000），回测两源合并去重。

覆盖：
a. 48h 外事件清理时被追加进 backtest_events 且字段完整（stocks/dir/t 等）
b. 超 BACKTEST_EVENTS_MAX 按时间裁最旧
c. _gist_save 超限时 seen 压缩后仍超限 → backtest_events 裁到最新 500 条
   （mock patch_gist_file 捕获 payload 断言）；500 条仍超限才 raise
d. signal_backtest 合并来源去重（同 t+title_norm 只留一条，档案优先）；
   缺 backtest_events 时降级仅用 pushed_events
e. _merge_state 本地+远端 backtest_events 并集不丢条目
"""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit  # 纯单元测试：mock 数据源，无网络无推送

BJT = timezone(timedelta(hours=8))


def _load_rtp():
    """以文件路径加载 scripts/real_time_push.py（scripts 非包，且 import 时会 chdir）"""
    cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(
            "real_time_push_bt", _PROJECT_ROOT / "scripts" / "real_time_push.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.chdir(cwd)


def _load_sb():
    """以文件路径加载 scripts/signal_backtest.py（与 rtp 同规则，隔离 import 副作用）"""
    cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location(
            "signal_backtest_bt", _PROJECT_ROOT / "scripts" / "signal_backtest.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.chdir(cwd)


rtp = _load_rtp()
sb = _load_sb()


def _now_str(offset_hours: float = 0) -> str:
    return (datetime.now(BJT) - timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")


def _event(i: int, offset_hours: float = 1.0, prefix: str = "标题",
           time_span: str = "hourly") -> dict:
    """完整字段的事件条目（与 pushed_events 生产结构一致）。

    t = now - (offset_hours + i*step)h → 同批内 i 越大越旧。time_span 控制步长：
    "hourly"（1h，用于窗口内外区分）、"2min"（2/60 h，320 条也全挤在 ~10.6h 窗口内，
    用于纯上限淘汰路径）。prefix 同时嵌入各字段，防 _event_sig_key 跨批碰撞。
    """
    step = 1.0 if time_span == "hourly" else 2.0 / 60.0
    return {
        "stocks": [f"{prefix}股{i}"], "entities": [f"{prefix}主体{i}"],
        "events": [f"{prefix}事件{i}"], "numbers": [f"{i}亿"],
        "sectors": [f"{prefix}板块{i}"], "scope": "sector",
        "title_norm": f"{prefix}{i}",
        "dir": "bullish",
        "t": _now_str(offset_hours + i * step),
    }


def _to_local_saver(monkeypatch, tmp_path):
    """save_state 强制本地 tmp 路径 + 清空 Gist/CI 环境；返回 save 函数"""
    monkeypatch.setenv("GIST_TOKEN", "")
    monkeypatch.setenv("GIST_ID", "")
    monkeypatch.delenv("CI", raising=False)
    state_path = tmp_path / "real_time_state.json"
    monkeypatch.setattr(rtp, "_state_path", lambda: state_path)

    def _save(state):
        rtp.save_state(state)
        return json.loads(state_path.read_text(encoding="utf-8"))
    return _save


# ============================================================
# a. 清理转存：48h 外事件进档案且字段完整
# ============================================================

class TestCleanupArchivesEvicted:
    def test_expired_event_archived_with_full_fields(self, monkeypatch, tmp_path):
        """48h 外被淘汰的已推事件必须原样转存 backtest_events，字段完整不丢失"""
        save = _to_local_saver(monkeypatch, tmp_path)
        old_event = _event(0, offset_hours=50, prefix="旧")   # 50h 前 → 出 48h 窗口
        new_event = _event(0, offset_hours=1, prefix="新")    # 1h 前 → 保留
        state = {"seen": {}, "pushed_events": [old_event, new_event]}
        saved = save(state)

        assert [e["title_norm"] for e in saved["pushed_events"]] == ["新0"]
        archived = [e for e in saved["backtest_events"] if e["title_norm"] == "旧0"]
        assert len(archived) == 1, "48h 外事件必须恰好转存一条进档案"
        # 字段完整：回测消费的全部字段与原条目一致
        for key in ("stocks", "entities", "events", "numbers", "sectors",
                    "scope", "title_norm", "dir", "t"):
            assert archived[0][key] == old_event[key], f"档案字段 {key} 必须原样保留"

    def test_in_window_events_not_archived(self, monkeypatch, tmp_path):
        """窗口内事件不进档案（只淘汰时转存，不重复全量复制）"""
        save = _to_local_saver(monkeypatch, tmp_path)
        state = {"seen": {}, "pushed_events": [_event(i, 1, "窗") for i in range(3)]}
        saved = save(state)
        assert len(saved["pushed_events"]) == 3, "窗口内事件必须保留"
        assert saved["backtest_events"] == [], "无淘汰时不得写入档案"

    def test_cap_evicted_events_also_archived(self, monkeypatch, tmp_path):
        """超 300 条上限被裁掉的窗口内事件同样转存（防上限挤丢可回测样本）。

        320 条全挤在窗口内（2 分钟间隔 ≈ 10.6h）→ 唯一淘汰路径是 300 上限：
        `_event` 中 i 越大越旧，t 升序排序后保留 i=0..299，最旧 20 条
       （i=300..319）进档案。
        """
        save = _to_local_saver(monkeypatch, tmp_path)
        events = [_event(i, offset_hours=1, prefix="档", time_span="2min") for i in range(320)]
        state = {"seen": {}, "pushed_events": events}
        saved = save(state)
        assert len(saved["pushed_events"]) == 300
        archived_titles = {e["title_norm"] for e in saved["backtest_events"]}
        assert {f"档{i}" for i in range(300, 320)} == archived_titles, \
            "上限裁掉的最旧 20 条必须恰好转存档案（不多不少）"

    def test_window_and_cap_eviction_paths_coexist(self, monkeypatch, tmp_path):
        """混合淘汰：48h 外走窗口路径 + 窗口内超 300 走上限路径，两者都进档案"""
        save = _to_local_saver(monkeypatch, tmp_path)
        out_of_window = _event(0, offset_hours=72, prefix="窗外")   # 72h 前
        capped = [_event(i, offset_hours=1, prefix="挤", time_span="2min") for i in range(320)]
        state = {"seen": {}, "pushed_events": [out_of_window] + capped}
        saved = save(state)
        archived_titles = {e["title_norm"] for e in saved["backtest_events"]}
        assert "窗外0" in archived_titles, "48h 外事件必须进档案"
        archived_titles = {e["title_norm"] for e in saved["backtest_events"]}
        assert archived_titles == {"窗外0"} | {f"挤{i}" for i in range(300, 320)}, \
            "上限裁掉的最旧 20 条必须恰好转存档案（不多不少）"
        assert all(e["title_norm"] != "窗外0" for e in saved["pushed_events"])


# ============================================================
# b. 档案上限：超 BACKTEST_EVENTS_MAX 按时间裁最旧
# ============================================================

class TestBacktestArchiveCap:
    def test_archive_pruned_to_max_keeping_newest(self, monkeypatch, tmp_path):
        save = _to_local_saver(monkeypatch, tmp_path)
        cap = rtp.BACKTEST_EVENTS_MAX
        # 档案已有 cap+50 条（i 大 → 旧）+ 50 条待淘汰（offset 更小 → 更新）
        archived = [_event(i, offset_hours=100, prefix="库") for i in range(cap + 50)]
        evicted = [_event(i, offset_hours=50, prefix="新") for i in range(50)]
        state = {"seen": {}, "pushed_events": evicted, "backtest_events": archived}
        saved = save(state)
        out = saved["backtest_events"]
        assert len(out) == cap, f"档案必须裁到 {cap} 条"
        # offset 50 的 50 条淘汰条目最新，必须全保留
        titles = {e["title_norm"] for e in out}
        assert {f"新{i}" for i in range(50)} <= titles, "最新淘汰条目必须保留"
        # 新增 50 条比库存更新，会再挤掉库存最旧 50 条（库2000..库2049）
        assert f"库{cap + 49}" not in titles, "最旧库存必须被裁掉"
        assert f"库{cap}" not in titles, "最旧库存必须被裁掉"
        assert f"库{cap - 51}" in titles, "未超限部分必须保留"
        assert f"库{cap - 50}" not in titles, "新增淘汰条目会挤掉库存边界条目"
        # 裁剪后按 t 升序
        ts = [e["t"] for e in out]
        assert ts == sorted(ts)

    def test_existing_archive_without_eviction_untouched(self, monkeypatch, tmp_path):
        """无淘汰轮次不重排已有档案（避免每轮全量重写）"""
        save = _to_local_saver(monkeypatch, tmp_path)
        # 乱序档案（无淘汰时不得被排序重写）
        archived = [_event(0, offset_hours=100, prefix="乱"),
                    _event(1, offset_hours=50, prefix="乱")]   # t 后大前小 → 非升序
        state = {"seen": {}, "backtest_events": archived,
                 "pushed_events": [_event(0, offset_hours=1, prefix="留")]}
        saved = save(state)
        assert saved["backtest_events"] == archived


# ============================================================
# c. Gist 体积守卫：seen 压缩后仍超限 → backtest_events 裁 500
# ============================================================

def _big_seen(n, title_len=300, t_base=None):
    t_base = t_base or datetime.now(BJT)
    return {f"fp{i:05d}": {"t": (t_base - timedelta(minutes=n - i)).strftime("%Y-%m-%d %H:%M:%S"),
                           "pushed": False, "title": "字" * title_len}
            for i in range(n)}


class TestGistSizeGuardTrimsArchive:
    def test_archive_trimmed_to_500_when_still_over_limit(self, monkeypatch):
        """seen 压缩后仍超限：backtest_events 裁到最新 500 条后成功上传"""
        captured = {}
        monkeypatch.setattr(rtp, "patch_gist_file",
                            lambda name, content, token, gid, **kw:
                            captured.update({"content": content}))
        # seen 1800 条 × 1.2KB ≈ 2.2MB（压缩后仍超限）+ 700 条档案
        state = {"seen": _big_seen(2500, title_len=400),
                 "pending": {}, "pushed_events": [], "candidate_events": [],
                 "watch_announce": [],
                 "backtest_events": [_event(i, offset_hours=100, prefix="档") for i in range(700)]}
        rtp._gist_save("tok", "gid", state)
        assert "content" in captured, "裁剪档案后必须成功上传"
        payload = json.loads(captured["content"])
        assert len(payload["backtest_events"]) == 500, "档案必须裁到 500 条"
        payload_bytes = len(captured["content"].encode("utf-8"))
        assert payload_bytes <= rtp.GIST_STATE_LIMIT_BYTES, "裁剪后必须低于上限"
        # _event 同批内 i 越大 t 越旧 → 裁剪保留 i=0..499（最新）
        titles = {e["title_norm"] for e in payload["backtest_events"]}
        assert "档0" in titles and "档499" in titles
        assert "档500" not in titles and "档699" not in titles

    def test_raises_when_archive_500_still_over_limit(self, monkeypatch):
        """裁完 500 条仍超限 → 必须报错（fail-stop 原则），不得上传"""
        monkeypatch.setattr(rtp, "patch_gist_file",
                            lambda name, content, token, gid, **kw: pytest.fail(
                                "裁剪后仍超限必须报错，不得上传"))
        # 500 条 × ~6KB（title_norm 2000 汉字）≈ 3MB → 裁后仍远超 950KB
        big_events = []
        for i in range(700):
            e = _event(i, offset_hours=100, prefix="巨")
            e["title_norm"] = "巨" * 2000
            big_events.append(e)
        state = {"seen": _big_seen(2500, title_len=400),
                 "pending": {}, "pushed_events": [], "candidate_events": [],
                 "watch_announce": [], "backtest_events": big_events}
        with pytest.raises(RuntimeError) as exc:
            rtp._gist_save("tok", "gid", state)
        assert "压缩前" in str(exc.value) and "压缩后" in str(exc.value)

    def test_small_archive_not_trimmed_when_seen_compress_suffices(self, monkeypatch):
        """seen 压缩后已达标：不进第二道裁剪，档案原样上传（≤500 条本不触发）"""
        captured = {}
        monkeypatch.setattr(rtp, "patch_gist_file",
                            lambda name, content, token, gid, **kw:
                            captured.update({"content": content}))
        archive = [_event(i, offset_hours=100, prefix="安") for i in range(600)]
        state = {"seen": _big_seen(2500, title_len=120),
                 "pending": {}, "pushed_events": [], "candidate_events": [],
                 "watch_announce": [], "backtest_events": archive}
        rtp._gist_save("tok", "gid", state)
        assert "content" in captured
        payload = json.loads(captured["content"])
        assert len(payload["backtest_events"]) == 600, "seen 压缩已达标时档案不得被裁"


# ============================================================
# d. signal_backtest 合并来源去重
# ============================================================

class TestBacktestMergeSources:
    def _state(self, archive, pushed):
        return {"backtest_events": archive, "pushed_events": pushed}

    def test_merge_dedupes_by_t_and_title(self):
        """同 (t, title_norm) 条目只留一条，backtest_events 优先"""
        a1 = {"t": "2026-09-05 10:00:00", "title_norm": "标题A", "dir": "bearish",
              "stocks": ["档案股"], "entities": [], "events": [], "numbers": [],
              "sectors": [], "scope": "stock"}
        a2 = {"t": "2026-09-06 10:00:00", "title_norm": "标题B", "dir": "bullish",
              "stocks": [], "entities": [], "events": [], "numbers": [],
              "sectors": [], "scope": "market"}
        p_dup = {"t": "2026-09-05 10:00:00", "title_norm": "标题A", "dir": "bullish",
                 "stocks": ["窗口股"], "entities": [], "events": [], "numbers": [],
                 "sectors": [], "scope": "stock"}
        p_only = {"t": "2026-09-08 10:00:00", "title_norm": "标题C", "dir": "neutral",
                  "stocks": [], "entities": [], "events": [], "numbers": [],
                  "sectors": [], "scope": "market"}
        merged = sb._merge_backtest_events(self._state([a1, a2], [p_dup, p_only]))
        assert len(merged) == 3, "同 (t, title_norm) 去重后 3 条"
        by_title = {e["title_norm"]: e for e in merged}
        assert by_title["标题A"]["stocks"] == ["档案股"], "重复条目必须保留档案侧"
        assert by_title["标题C"]["stocks"] == [], "窗口独有条目必须保留"

    def test_missing_backtest_events_falls_back_to_pushed(self):
        """旧状态文件无 backtest_events 键 → 降级仅用 pushed_events"""
        pushed = [{"t": "2026-09-08 10:00:00", "title_norm": "窗口事件",
                   "dir": "bullish"}]
        merged = sb._merge_backtest_events({"pushed_events": pushed})
        assert merged == pushed

    def test_empty_state_returns_empty(self):
        assert sb._merge_backtest_events({}) == []
        assert sb._merge_backtest_events({"backtest_events": [], "pushed_events": []}) == []

    def test_winrate_reads_merged_sources(self, monkeypatch):
        """compute_winrate 必须走合并来源（档案事件参与统计而非被忽略）"""
        monkeypatch.setattr(sb, "_load_realtime_state",
                            lambda: {"backtest_events": [
                                {"t": "2026-09-05 10:00:00", "title_norm": "档案事件",
                                 "dir": "bullish", "stocks": ["股"], "entities": [],
                                 "events": [], "numbers": [], "sectors": [],
                                 "scope": "stock"}],
                                "pushed_events": []})
        # backtest() 会走行情/解析网络 → 桩掉，只验证来源合并生效
        monkeypatch.setattr(sb, "backtest", lambda events, days=30: {"overall": {"n": len(events)}})
        out = sb.compute_winrate(days=30)
        assert out["n"] == 1, "档案事件必须参与回测统计"


# ============================================================
# e. _merge_state：本地+远端 backtest_events 并集不丢条目
# ============================================================

class TestMergeStateBacktestUnion:
    def test_local_remote_union_no_loss(self):
        """本地/远端并发写：backtest_events 取并集（同 key 远端优先），不丢条目"""
        local_only = {"entities": ["L"], "events": [], "numbers": [],
                      "title_norm": "本地独有", "t": "2026-09-05 10:00:00"}
        remote_only = {"entities": ["R"], "events": [], "numbers": [],
                       "title_norm": "远端独有", "t": "2026-09-06 10:00:00"}
        same_key_local = {"stocks": ["本地股"], "entities": ["同主体"], "events": [], "numbers": [],
                          "title_norm": "同键", "t": "2026-09-07 10:00:00"}
        same_key_remote = {"stocks": ["远端股"], "entities": ["同主体"], "events": [], "numbers": [],
                           "title_norm": "同键", "t": "2026-09-07 10:00:00"}
        local = {"seen": {}, "pending": {}, "pushed_events": [], "watch_announce": [],
                 "backtest_events": [local_only, same_key_local]}
        remote = {"seen": {}, "pending": {}, "pushed_events": [], "watch_announce": [],
                  "backtest_events": [remote_only, same_key_remote]}
        merged = rtp._merge_state(local, remote)
        titles = sorted(e["title_norm"] for e in merged["backtest_events"])
        assert titles == ["同键", "本地独有", "远端独有"], "并集不得丢条目"
        same = [e for e in merged["backtest_events"] if e["title_norm"] == "同键"]
        assert len(same) == 1 and same[0]["stocks"] == ["远端股"], \
            "同 key 冲突时远端优先（与 pushed_events 口径一致）"

    def test_remote_missing_backtest_events(self):
        """远端旧状态无档案键 → 本地档案原样保留"""
        local = {"seen": {}, "pending": {}, "pushed_events": [], "watch_announce": [],
                 "backtest_events": [{"entities": ["L"], "events": [], "numbers": [],
                                      "title_norm": "本地", "t": "2026-09-05 10:00:00"}]}
        merged = rtp._merge_state(local, {"seen": {}, "pending": {}, "pushed_events": []})
        assert [e["title_norm"] for e in merged["backtest_events"]] == ["本地"]
