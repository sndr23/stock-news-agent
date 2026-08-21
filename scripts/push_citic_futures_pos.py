#!/usr/bin/env python3
"""中信期货 中金所股指期货持仓日报推送

每天收盘后（中金所持仓排名公布后，定时 17:00 起）运行，把中信期货在
IF/IH/IC/IM 上的每日净持仓变化量（全合约口径）推送到微信。
交易日当天数据未公布时退出码 2（workflow 自动重试，直至数据就绪）。

口径: 全合约汇总 —— 每个品种所有挂牌合约的中信期货多单/空单加总，
      净增减 = 多单增减合计 - 空单增减合计，再跨四品种求和。
      净增减 > 0 = 净加多单; 净增减 < 0 = 净加空单。

数据源: 中金所官网持仓排名 XML
  http://www.cffex.com.cn/sj/ccpm/{YYYYMM}/{DD}/{product}.xml
  datatypeid: 0=成交量, 1=持买单量(多单), 2=持卖单量(空单)  [官方 ccpm.js 确认]

去重: 每个交易日只推一次。云端用 Gist（GIST_TOKEN/GIST_ID），本地用文件。

用法:
  python scripts/push_citic_futures_pos.py            # 正常推送
  python scripts/push_citic_futures_pos.py --dry-run  # 只打印不推送
  python scripts/push_citic_futures_pos.py --date 20260821  # 指定交易日
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .ENV（与 src.config 一致，但避免触发其日志/代理副作用）
load_dotenv(PROJECT_ROOT / ".ENV")

from src.tools.push import push_via_pushplus  # noqa: E402

logger = logging.getLogger("citic_pos_push")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(_h)

BASE = "http://www.cffex.com.cn/sj/ccpm/{ym}/{d}/{product}.xml"
PRODUCTS = ["IF", "IH", "IC", "IM"]
MEMBER = "中信期货"
GIST_STATE_FILENAME = "citic_pos_state.json"
LOCAL_STATE_PATH = PROJECT_ROOT / "logs" / "citic_pos_state.json"


# ============================================================
# CFFEX 数据抓取
# ============================================================
def fetch_product(product: str, d: date):
    url = BASE.format(ym=d.strftime("%Y%m"), d=d.strftime("%d"), product=product)
    try:
        r = requests.get(url, timeout=15)
    except Exception as e:
        logger.warning(f"{product} {d} 请求失败: {e}")
        return None
    if r.status_code != 200 or len(r.text) < 500:
        return None
    return r


def parse_xml(text: str):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    rows = []
    for data in root.findall("data"):
        rows.append(
            {
                "dtype": data.find("datatypeid").text,
                "name": data.find("shortname").text,
                "var": int(data.find("varvolume").text),
            }
        )
    return rows


def _is_trading_day(d: date) -> bool:
    """A股交易日判断（与 real_time_push.py 同口径）"""
    try:
        import chinese_calendar  # type: ignore
        return chinese_calendar.is_workday(d)
    except ImportError:
        return d.weekday() < 5


def resolve_target_day() -> date:
    """确定要推送的交易日。

    交易日当天数据未公布 → 返回 None（调用方以退出码 2 触发 workflow 重试）；
    非交易日（周末/节假日）→ 回退最近一个已有数据的交易日（Gist 去重兜底跳过）。
    """
    today = date.today()
    if _is_trading_day(today):
        if fetch_product("IF", today) is not None:
            return today
        logger.warning(f"{today} 为交易日但持仓排名数据未公布，等待重试")
        return None
    d = today - timedelta(days=1)
    for _ in range(7):
        if fetch_product("IF", d) is not None:
            return d
        d -= timedelta(days=1)
    raise RuntimeError("近 7 天均未找到中金所持仓排名数据")


def compute_daily(d: date) -> dict:
    """计算指定交易日中信期货全合约口径的净增减"""
    result = {}
    for p in PRODUCTS:
        resp = fetch_product(p, d)
        if resp is None:
            logger.warning(f"{p} {d} 无数据，跳过")
            continue
        buy_var = sell_var = 0
        for r in parse_xml(resp.text):
            if r["name"].startswith(MEMBER):
                if r["dtype"] == "1":
                    buy_var += r["var"]
                elif r["dtype"] == "2":
                    sell_var += r["var"]
        result[p] = {"buy_var": buy_var, "sell_var": sell_var, "net_var": buy_var - sell_var}
    return result


def compute_recent(end: date, n: int = 5) -> list:
    """最近 n 个交易日的净增减（用于趋势展示）"""
    out = []
    d = end
    while len(out) < n and d >= end - timedelta(days=30):
        daily = compute_daily(d)
        if daily:
            total = sum(c["net_var"] for c in daily.values())
            out.append({"day": d.strftime("%m-%d"), "total": total, "daily": daily})
        d -= timedelta(days=1)
    return out


# ============================================================
# 状态持久化（Gist 云端 / 本地文件）
# ============================================================
def _load_state() -> dict:
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            headers = {
                "Authorization": f"token {gist_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "stock-news-agent-citic-pos",
            }
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            fobj = files.get(GIST_STATE_FILENAME)
            if fobj is not None:
                return json.loads(fobj.get("content") or "{}")
            return {}
        except Exception as e:
            logger.warning(f"Gist 状态读取失败，回退本地: {e}")
    if LOCAL_STATE_PATH.exists():
        try:
            return json.loads(LOCAL_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            headers = {
                "Authorization": f"token {gist_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "stock-news-agent-citic-pos",
            }
            payload = {"files": {GIST_STATE_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}}
            resp = requests.patch(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            logger.info("状态已写入 Gist")
            return
        except Exception as e:
            logger.warning(f"Gist 状态写入失败，回退本地: {e}")
    LOCAL_STATE_PATH.parent.mkdir(exist_ok=True)
    LOCAL_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"状态已写入本地 {LOCAL_STATE_PATH}")


# ============================================================
# 消息格式化
# ============================================================
def format_message(d: date, daily: dict, recent: list) -> str:
    total = sum(c["net_var"] for c in daily.values())
    direction = "净加空单" if total < 0 else "净加多单"
    lines = [
        "📊 中信期货股指期货持仓日报",
        f"📅 {d.strftime('%Y-%m-%d')}（全合约口径）",
        "",
        "今日净持仓变化（手）：",
    ]
    name_map = {"IF": "IF沪深300", "IH": "IH上证50", "IC": "IC中证500", "IM": "IM中证1000"}
    for p in PRODUCTS:
        if p in daily:
            c = daily[p]
            lines.append(f"{name_map[p]}: {c['net_var']:+d}")
    lines += [
        "━━━━━━━━━━━━",
        f"合计: {total:+d} 手（{direction}）",
        "",
        "近5日净增减（手）：",
    ]
    for r in recent:
        lines.append(f"{r['day']}: {r['total']:+d}")
    lines += [
        "",
        "数据来源：中金所成交持仓排名",
    ]
    return "\n".join(lines)


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="中信期货持仓日报推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印不推送")
    parser.add_argument("--date", type=str, default="", help="指定交易日 YYYYMMDD（默认取最近交易日）")
    parser.add_argument("--force", action="store_true", help="忽略去重强制推送")
    args = parser.parse_args()

    if args.date:
        d = datetime.strptime(args.date, "%Y%m%d").date()
    else:
        d = resolve_target_day()
        if d is None:
            logger.info("交易日数据未公布，退出码 2 等待 workflow 重试")
            return 2

    daily = compute_daily(d)
    if not daily:
        logger.error(f"{d} 无任何品种数据，跳过")
        return 1

    # 去重: 每个交易日只推一次
    state = _load_state()
    last_pushed = state.get("last_pushed_day", "")
    if not args.force and last_pushed == d.strftime("%Y%m%d"):
        logger.info(f"{d} 已推送过（last_pushed_day={last_pushed}），跳过")
        return 0

    recent = compute_recent(d, n=5)
    msg = format_message(d, daily, recent)

    if args.dry_run:
        print("===== DRY RUN 消息预览 =====")
        print(msg)
        print("============================")
        return 0

    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if not token:
        logger.error("未配置 PUSHPLUS_TOKEN，无法推送")
        return 1

    title = f"中信期货持仓日报 {d.strftime('%m-%d')}"
    result = push_via_pushplus(token, title, msg)
    if result.get("code") != 200:
        logger.error(f"推送失败: {result}")
        return 1

    state["last_pushed_day"] = d.strftime("%Y%m%d")
    _save_state(state)
    logger.info(f"推送成功: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
