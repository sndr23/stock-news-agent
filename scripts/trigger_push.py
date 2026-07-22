#!/usr/bin/env python3
"""本地定时触发脚本 — 通过 GitHub API 触发 workflow_dispatch
配合 Windows 计划任务使用，实现零延迟定时推送

用法（Windows 计划任务自动调用）:
    python trigger_push.py --slot morning
"""

import argparse
import json
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# 禁用代理（本地网络直连 GitHub API，避免代理软件未启动时连接失败）
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(proxy_handler)
urllib.request.install_opener(opener)

BJT = timezone(timedelta(hours=8))

PAT = "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"
REPO = "sndr23/stock-news-agent"
WORKFLOW_FILE = "daily-push.yml"


def trigger(slot: str) -> bool:
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    body = json.dumps({"ref": "main", "inputs": {"slot": slot}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 204):
                print(f"[{now}] dispatch OK: slot={slot}")
                return True
            else:
                print(f"[{now}] dispatch UNEXPECTED status={resp.status}: slot={slot}")
                return False
    except urllib.error.HTTPError as e:
        print(f"[{now}] dispatch FAILED: slot={slot} status={e.code} error={e.reason}")
        return False
    except Exception as e:
        print(f"[{now}] dispatch ERROR: slot={slot} error={e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="触发 GitHub Actions 推送")
    parser.add_argument(
        "--slot",
        required=True,
        choices=["morning", "noon", "afternoon", "night"],
        help="推送时段",
    )
    args = parser.parse_args()
    ok = trigger(args.slot)
    sys.exit(0 if ok else 1)
