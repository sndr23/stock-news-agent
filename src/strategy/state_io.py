"""可靠写入跨轮运行状态的小型文件工具。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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


def _gist_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def patch_gist_file(filename: str, content: str, token: str, gist_id: str,
                    *, timeout: float = 20.0, max_attempts: int = 3) -> None:
    """带 ETag 乐观锁的 Gist 单文件写入（防写-写竞态覆盖）。

    背景（2026-08-13 状态丢失事故）：Gist 状态是"读-改-写"模式，两个写端
    并发时后写会覆盖先写，曾冲掉 4759 条 seen / 200 pending / 62 pushed 去重记录。
    读取侧的 fail-stop 已修复（读失败禁止空状态回写），本函数补上写入侧。

    机制：先 GET 取目标 Gist 的 ETag（版本标识），再 PATCH 时带 ``If-Match``；
    若期间被其他写端更新过，GitHub 返回 412，本函数**放弃写入并报错**
    （fail-safe：宁可本轮状态未落盘，也不覆盖他人更新）。只提交目标文件，
    不整包回写，避免波及其他状态文件。

    Raises:
        RuntimeError: 配置不全、网络失败或版本冲突（412）重试耗尽。
    """
    import requests

    if not token or not gist_id:
        raise RuntimeError("Gist 配置缺失（token/gist_id 为空），拒绝静默本地降级")
    base = f"https://api.github.com/gists/{gist_id}"
    headers = _gist_headers(token)
    last_error = None
    for attempt in range(max_attempts):
        try:
            # 步骤 1：取当前版本 ETag（追加时间戳绕过 CDN 缓存，与读取侧一致）
            ts = int(time.time() * 1000)
            resp = requests.get(f"{base}?ts={ts}", headers=headers, timeout=timeout)
            resp.raise_for_status()
            etag = resp.headers.get("ETag") or (resp.json().get("updated_at") or "")
            if not etag:
                raise RuntimeError("Gist 未返回 ETag，无法启用乐观锁")

            # 步骤 2：只提交目标文件，带 If-Match 版本校验
            payload = {"files": {filename: {"content": content}}}
            patch_headers = {**headers, "If-Match": etag}
            presp = requests.patch(base, json=payload, headers=patch_headers,
                                   timeout=timeout)
            if presp.status_code == 412:
                last_error = RuntimeError(
                    f"Gist 版本冲突（412）：{filename} 在读取后已被其他写端更新，"
                    f"放弃本次写入以免覆盖（第 {attempt + 1}/{max_attempts} 次）")
                logger.warning(str(last_error))
                if attempt < max_attempts - 1:
                    time.sleep(1)
                    continue
                raise last_error
            presp.raise_for_status()
            return
        except RuntimeError:
            raise
        except Exception as e:  # 网络/HTTP 瞬时错误 → 重试
            last_error = e
            if attempt < max_attempts - 1:
                logger.warning("Gist 乐观锁写入第%d次失败: %s，1s 后重试",
                               attempt + 1, e)
                time.sleep(1)
    raise RuntimeError(
        f"Gist 乐观锁写入失败（已重试 {max_attempts - 1} 次）: {last_error}")
