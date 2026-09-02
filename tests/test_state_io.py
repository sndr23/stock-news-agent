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


class _FakeUrlResp:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_read_gist_json_falls_back_to_raw_url_when_content_is_truncated(monkeypatch):
    """Gist content 截断时必须下载 raw_url 的完整 JSON。"""
    metadata = {
        "files": {
            "real_time_state.json": {
                "content": '{"pushed_events": [',
                "truncated": True,
                "raw_url": "https://raw.example/real_time_state.json",
            }
        }
    }
    calls = []

    def fake_urlopen(req, timeout=15):
        url = req.full_url
        calls.append(url)
        if "raw.example" in url:
            return _FakeUrlResp('{"pushed_events": [{"id": 1}]}')
        return _FakeUrlResp(json.dumps(metadata))

    monkeypatch.setattr(state_io, "urlopen", fake_urlopen)

    out = state_io.read_gist_json("real_time_state.json", "tok", "gid", strict=True)

    assert out == {"pushed_events": [{"id": 1}]}
    assert len(calls) == 2
    assert "ts=" in calls[0] and "ts=" in calls[1]


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


# ---------------- Gist 写入（2026-08-31 修复：禁止 If-Match 条件请求） ----------------

class _FakeResp:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _requests
            raise _requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequests:
    """记录 PATCH 调用，可模拟持续 HTTP 错误。"""

    def __init__(self, patch_status=200):
        self.calls = []
        self.patch_status = patch_status

    def patch(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PATCH", headers, json))
        return _FakeResp(self.patch_status, headers={})


def test_patch_gist_file_sends_no_conditional_headers(monkeypatch):
    """PATCH 不得携带 If-Match：Gists API 不支持条件请求，2026-08-29 起带该头
    一律 400，曾导致云端三条链路全停 62+ 小时（回归护栏）。"""
    fake = _FakeRequests()
    monkeypatch.setattr("requests.patch", fake.patch)

    state_io.patch_gist_file("real_time_state.json", '{"seen": 1}',
                             "tok", "gid", max_attempts=2)

    methods = [c[0] for c in fake.calls]
    assert methods == ["PATCH"]
    patch_headers = fake.calls[0][1]
    assert "If-Match" not in patch_headers, "Gist PATCH 禁止 If-Match（400）"
    assert "If-None-Match" not in patch_headers
    # 只提交目标文件，不整包回写（避免波及其他状态文件）
    assert list(fake.calls[0][2]["files"]) == ["real_time_state.json"]


def test_patch_gist_file_gives_up_after_retries(monkeypatch):
    """持续 HTTP 错误必须重试后放弃并报错，不得静默吞掉。"""
    fake = _FakeRequests(patch_status=500)
    monkeypatch.setattr("requests.patch", fake.patch)

    with pytest.raises(RuntimeError, match="写入失败"):
        state_io.patch_gist_file("factor_state.json", '{"x": 1}',
                                 "tok", "gid", max_attempts=2)
    assert len(fake.calls) == 2


def test_patch_gist_file_rejects_missing_config(monkeypatch):
    """配置缺失时必须报错，禁止静默降级本地。"""
    with pytest.raises(RuntimeError, match="配置缺失"):
        state_io.patch_gist_file("x.json", "{}", "", "gid")
    with pytest.raises(RuntimeError, match="配置缺失"):
        state_io.patch_gist_file("x.json", "{}", "tok", "")
