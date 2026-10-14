"""可靠写入跨轮运行状态的小型文件工具。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

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


def read_gist_json(filename: str, token: str, gist_id: str, *,
                   strict: bool = False, user_agent: str = "stock-news-agent-state",
                   timeout: float = 15.0) -> dict:
    """读取 Gist JSON，并在 API content 被截断时回退 raw_url。

    Gist 元数据接口对大文件只返回截断的 ``content``；raw_url 才是完整文件。
    ``strict`` 保留各调用方现有语义：严格状态读取失败直接抛错，非严格读取返回空对象。
    """
    if not token or not gist_id:
        if strict:
            raise RuntimeError("Gist 配置缺失，拒绝静默回退本地状态")
        return {}

    filename = str(filename)
    headers = {**_gist_headers(token), "User-Agent": user_agent}
    try:
        api_url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
        req = Request(api_url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            gist = json.loads(resp.read().decode("utf-8"))
        fobj = (gist.get("files") or {}).get(filename)
        if fobj is None:
            return {}

        content = fobj.get("content")
        raw_url = fobj.get("raw_url")
        state = None
        parse_error = None
        if not fobj.get("truncated") and isinstance(content, str) and content.strip():
            try:
                state = json.loads(content)
            except (json.JSONDecodeError, TypeError) as e:
                parse_error = e

        if state is None:
            if not raw_url:
                if parse_error is not None:
                    raise parse_error
                raise ValueError(f"Gist 状态文件 {filename} 内容为空")
            sep = "&" if "?" in raw_url else "?"
            raw_req = Request(
                f"{raw_url}{sep}ts={int(time.time() * 1000)}",
                headers=headers,
            )
            with urlopen(raw_req, timeout=max(timeout, 30.0)) as resp:
                state = json.loads(resp.read().decode("utf-8"))
            logger.warning("Gist 文件 %s 使用 raw_url 回退读取完整内容", filename)

        if not isinstance(state, dict):
            raise ValueError(f"Gist 状态文件 {filename} 根节点不是对象")
        return state
    except Exception as e:
        logger.warning("Gist 状态文件 %s 读取失败: %s", filename, type(e).__name__)
        if strict:
            raise RuntimeError(
                f"Gist 状态文件 {filename} 读取失败，拒绝回退本地") from e
        return {}


def patch_gist_file(filename: str, content: str, token: str, gist_id: str,
                    *, timeout: float = 20.0, max_attempts: int = 3) -> None:
    """Gist 单文件写入（2026-08-31 修复：禁止 If-Match 条件请求）。

    背景1（2026-08-13 状态丢失事故）：Gist 状态是"读-改-写"模式，两个写端
    并发时后写会覆盖先写，曾冲掉 4759 条 seen / 200 pending / 62 pushed 去重记录。
    读取侧的 fail-stop 已修复（读失败禁止空状态回写）；本函数只提交目标文件、
    不整包回写，将覆盖面限制在单文件内。

    背景2（2026-08-29 写入全挂事故）：ccbe890 曾用 If-Match（ETag）实现
    乐观锁，但 **GitHub Gists API 不支持条件请求**——2026-08-29 起对所有带
    ``If-Match`` 的 PATCH 返回 400 Bad Request，导致云端状态写入连续失败、
    资讯推送/因子采集/仓位信号三条链路全停 62+ 小时。Gist API 没有原生
    并发控制，写-写竞态的防护依赖架构层：各 workflow 独立 concurrency
    group（同组串行）+ 单文件提交（跨组各写各的文件，互不覆盖）。
    ⚠️ 任何情况下不得给 Gist PATCH 添加 If-Match/If-None-Match 头。

    Raises:
        RuntimeError: 配置不全、网络失败或持续 HTTP 错误（重试耗尽）。
    """
    import requests

    if not token or not gist_id:
        raise RuntimeError("Gist 配置缺失（token/gist_id 为空），拒绝静默本地降级")
    base = f"https://api.github.com/gists/{gist_id}"
    headers = _gist_headers(token)
    last_error = None
    for attempt in range(max_attempts):
        try:
            # 只提交目标文件，不整包回写，避免波及其他状态文件
            payload = {"files": {filename: {"content": content}}}
            presp = requests.patch(base, json=payload, headers=headers,
                                   timeout=timeout)
            presp.raise_for_status()
            return
        except RuntimeError:
            raise
        except Exception as e:  # 网络/HTTP 瞬时错误 → 重试
            last_error = e
            if attempt < max_attempts - 1:
                logger.warning("Gist 写入第%d次失败: %s，1s 后重试",
                               attempt + 1, e)
                time.sleep(1)
    raise RuntimeError(
        f"Gist 写入失败（已重试 {max_attempts - 1} 次）: {last_error}")
