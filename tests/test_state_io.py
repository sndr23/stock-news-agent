"""状态文件原子写入测试（不触网）。"""
import json
import sys
from pathlib import Path

import pytest

from src.strategy import state_io

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import run_chinext_timing as rct  # noqa: E402

pytestmark = pytest.mark.unit


def test_atomic_write_json_writes_complete_document(tmp_path):
    """正常写入应得到完整、可解析的 JSON 文件。"""
    path = tmp_path / "state.json"

    state_io.atomic_write_json(path, {"position": 0.6, "history": [1, 2]})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "position": 0.6, "history": [1, 2]
    }


def test_atomic_write_json_preserves_old_file_when_replace_fails(monkeypatch, tmp_path):
    """原子替换失败时，旧状态文件不得被半截内容覆盖。"""
    path = tmp_path / "state.json"
    path.write_text('{"position": 1.0}', encoding="utf-8")

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(state_io.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        state_io.atomic_write_json(path, {"position": 0.0})

    assert json.loads(path.read_text(encoding="utf-8")) == {"position": 1.0}


@pytest.mark.parametrize(
    ("token", "gist_id"),
    [("tok123", ""), ("", "gid123")],
)
def test_partial_gist_config_does_not_fallback_to_local_state(
        monkeypatch, tmp_path, token, gist_id):
    """只配置一个 Gist 变量时必须报错，禁止静默读取本地旧状态。"""
    monkeypatch.setenv("GIST_TOKEN", token)
    monkeypatch.setenv("GIST_ID", gist_id)
    path = tmp_path / "chinext_timing_state.json"
    path.write_text('{"last_date": "2026-08-27", "position": 1.0}',
                    encoding="utf-8")
    monkeypatch.setattr(rct, "_LOCAL_STATE_PATH", path)

    with pytest.raises(RuntimeError, match="GIST_TOKEN/GIST_ID.*同时"):
        rct.load_state()


# ---------------- Gist ETag 乐观锁（P0-2，2026-08-29） ----------------

class _FakeResp:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequests:
    """记录 GET/PATCH 调用，可模拟 412 版本冲突。"""

    def __init__(self, patch_status=200):
        self.calls = []
        self.patch_status = patch_status

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", headers))
        return _FakeResp(200, headers={"ETag": 'W/"v1"'}, payload={"updated_at": "x"})

    def patch(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PATCH", headers, json))
        if self.patch_status == 412:
            return _FakeResp(412, headers={})
        return _FakeResp(200, headers={})


def test_patch_gist_file_sends_if_match_header(monkeypatch):
    """写入必须带 If-Match（ETag），否则并发写会互相覆盖。"""
    fake = _FakeRequests()
    monkeypatch.setattr("requests.get", fake.get)
    monkeypatch.setattr("requests.patch", fake.patch)

    state_io.patch_gist_file("real_time_state.json", '{"seen": 1}',
                             "tok", "gid", max_attempts=2)

    methods = [c[0] for c in fake.calls]
    assert methods == ["GET", "PATCH"]
    patch_headers = fake.calls[1][1]
    assert patch_headers.get("If-Match") == 'W/"v1"', "PATCH 必须带 ETag 版本校验"
    # 只提交目标文件，不整包回写（避免波及其他状态文件）
    assert list(fake.calls[1][2]["files"]) == ["real_time_state.json"]


def test_patch_gist_file_aborts_on_version_conflict(monkeypatch):
    """412 版本冲突必须放弃写入并报错，绝不覆盖其他写端的更新。"""
    fake = _FakeRequests(patch_status=412)
    monkeypatch.setattr("requests.get", fake.get)
    monkeypatch.setattr("requests.patch", fake.patch)

    with pytest.raises(RuntimeError, match="版本冲突"):
        state_io.patch_gist_file("factor_state.json", '{"x": 1}',
                                 "tok", "gid", max_attempts=2)
    # 重试耗尽后必须放弃，不得继续提交覆盖写
    assert sum(1 for c in fake.calls if c[0] == "PATCH") == 2


def test_patch_gist_file_rejects_missing_config(monkeypatch):
    """配置缺失时必须报错，禁止静默降级本地。"""
    with pytest.raises(RuntimeError, match="配置缺失"):
        state_io.patch_gist_file("x.json", "{}", "", "gid")
    with pytest.raises(RuntimeError, match="配置缺失"):
        state_io.patch_gist_file("x.json", "{}", "tok", "")
