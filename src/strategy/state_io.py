"""可靠写入跨轮运行状态的小型文件工具。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def get_gist_config() -> tuple[str, str]:
    """读取 Gist 配置；两个变量必须同时存在，否则拒绝静默本地降级。"""
    token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if bool(token) != bool(gist_id):
        raise RuntimeError(
            "GIST_TOKEN/GIST_ID 必须同时配置；当前配置不完整，拒绝回退本地状态"
        )
    return token, gist_id


def atomic_write_json(path: Path, value: Any, *, indent: int = 2) -> None:
    """将 JSON 写入同目录临时文件后原子替换目标文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=indent)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
