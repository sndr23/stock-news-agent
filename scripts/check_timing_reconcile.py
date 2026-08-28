# -*- coding: utf-8 -*-
"""
创业板仓位信号·周对账脚本（check_timing_reconcile.py）
====================================================
只读对账：核对本地 logs/chinext_timing_state.json 与云端 Gist 状态是否一致
（last_date / position / history 条数）。仅审计，不写入 Gist——
状态 Gist 是单写端（chainext-timing workflow 每次推送写），本脚本绝不并发写，
避免与单写端冲突（对齐平台多写防护最佳实践）。

用法：
  python scripts/check_timing_reconcile.py          # 默认对比本地 logs 与 Gist
  python scripts/check_timing_reconcile.py --gist-only  # 只看Gist，不比本地
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from src.strategy.state_io import get_gist_config  # noqa: E402

TIMING_STATE_FILENAME = "chinext_timing_state.json"
_LOCAL_STATE_PATH = PROJECT_ROOT / "logs" / TIMING_STATE_FILENAME


def _load_local_env() -> None:
    """加载本地运行配置；已有进程环境变量优先于项目 .ENV。"""
    load_dotenv(PROJECT_ROOT / ".ENV", override=False)


def _read_gist(token: str, gist_id: str):
    url = f"https://api.github.com/gists/{gist_id}"
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "stock-news-agent-chinext-timing"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    f = (data.get("files") or {}).get(TIMING_STATE_FILENAME)
    if not f:
        return {}
    state = json.loads(f.get("content") or "{}")
    if not isinstance(state, dict):
        raise ValueError("Gist 状态根节点不是对象")
    return state


def _read_local() -> dict:
    try:
        return json.loads(_LOCAL_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _summary(st: dict, tag: str) -> str:
    hist = st.get("history") or []
    return (f"{tag}: last_date={st.get('last_date')} position="
            f"{st.get('position')} last_score={st.get('last_score')} "
            f"history={len(hist)} 条")


def main():
    _load_local_env()
    ap = argparse.ArgumentParser(description="创业板仓位信号状态对账（只读，绝不写Gist）")
    ap.add_argument("--gist-only", action="store_true", help="只读取并显示Gist状态")
    args = ap.parse_args()

    try:
        token, gist_id = get_gist_config()
    except RuntimeError as e:
        print(f"ABORT：{e}")
        return
    if not (token and gist_id):
        if args.gist_only:
            print("ABORT：--gist-only 需要配置 GIST_TOKEN/GIST_ID")
            return
        local = _read_local()
        print("未配置 GIST_TOKEN/GIST_ID，仅本地状态：")
        print("  " + _summary(local, "local"))
        return

    try:
        gist = _read_gist(token, gist_id)
    except Exception as e:
        print(f"ABORT：云端状态读取失败（{type(e).__name__}）")
        return
    if args.gist_only:
        print(_summary(gist, "gist "))
        if not gist:
            print("ABORT：Gist 无此文件，云状态缺失——需检查 chinext-timing workflow 是否成功推送过")
        return

    local = _read_local()
    print(_summary(local, "local"))
    print(_summary(gist, "gist "))

    if not local and not gist:
        print("ABORT：本地与 Gist 均为空（尚未首次推送？）")
        return
    if not gist:
        print("ABORT：Gist 无此文件，云状态缺失——需检查 chainext-timing workflow 是否成功推送过")
        return
    # 以 Gist 为准（单写端），本地仅作缓存降级；不一致时本地可安全被Gist覆盖
    if local.get("last_date") != gist.get("last_date"):
        print(f"WARN：本地 last_date={local.get('last_date')} 与 Gist={gist.get('last_date')} "
              f"不一致（以 Gist 为准，本地缓存可覆盖）")
    else:
        lpos, gpos = local.get("position"), gist.get("position")
        lhist, ghist = len(local.get("history") or []), len(gist.get("history") or [])
        if lpos == gpos and lhist == ghist:
            print("OK：本地与 Gist 状态一致")
        else:
            print("WARN：last_date 相同但 position/history 不一致 "
                  f"(position {lpos} vs {gpos}; history {lhist} vs {ghist})")


if __name__ == "__main__":
    main()
