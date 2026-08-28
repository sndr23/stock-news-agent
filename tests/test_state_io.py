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
