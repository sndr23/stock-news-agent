# -*- coding: utf-8 -*-
"""
量化因子采集器（factor_collector.py）
====================================================
定位：与 real_time_push.py（资讯事件流）并列的第二采集入口——"量化因子流"。
     资讯是"事件流"（去重用事件指纹），因子是"状态/时序"（去重用冷却时间），
     两者出口共用 src/tools/push.py 推送（红涨绿跌 + emoji，微信端体验一致）。

覆盖维度（2026-08-14 起，用户明确先补两维）：
1. 技术面（指数级）：上证指数 / 创业板指的均线(MA5/10/20/60)、动量(5/20日涨跌幅)、
   突破(20日新高/新低)、放量(成交额 vs 5/20日均量)——反映市场整体，非个股。
2. 宏观流动性：股指期货基差(IF/IC/IM/IH 主力连续 vs 对应现货指数)、
   汇率(美元/日元、美元/在岸人民币)——套息交易与中性策略对冲成本。

数据源（均为免费公开 HTTP 接口，不依赖通达信 MCP 会话）：
- 腾讯行情  qt.gtimg.cn          → 指数实时（上证/创业板/宽基现货）
- 东方财富  push2his.eastmoney   → 指数日K（算均线/动量/突破/放量）
- 新浪外汇  hq.sinajs.cn/fx_     → 汇率（美元/日元、美元/人民币）
- 新浪期货  hq.sinajs.cn/nf_     → 股指期货主力连续（算基差）

用法：
  python scripts/factor_collector.py --dry-run   # 只采集+计算+打印快照，不推送
  python scripts/factor_collector.py --push      # 打印快照；有异动且过冷却则推微信
  python scripts/factor_collector.py --loop      # 常驻：交易时段每 RT_POLL_SECONDS(默认300s)
                                                 # 高频轮询，非交易时段每 RT_POLL_IDLE_SECONDS(默认1800s)
                                                 # 低频轮询（实时因子盘中分分钟变化，30分钟一轮会滞后）
"""
import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".ENV")

from src.tools.push import push_via_wecom, push_via_pushplus  # 推送（含重试，复用现有出口）

logger = logging.getLogger("factor_collector")

# ============================================================
# 配置
# ============================================================
# 技术面指数（腾讯代码 → 新浪 symbol）。上证/创业板为主（用户长期偏好），宽基用于基差。
# 注：K 线用新浪 getKLineData（稳定无反爬）；实时行情用腾讯（含成交额）。
INDEXES = {
    "上证指数": {"tencent": "sh000001", "sina": "sh000001"},
    "创业板指": {"tencent": "sz399006", "sina": "sz399006"},
    "沪深300":  {"tencent": "sh000300", "sina": "sh000300"},
    "中证500":  {"tencent": "sh000905", "sina": "sh000905"},
    "中证1000": {"tencent": "sh000852", "sina": "sh000852"},
    "上证50":   {"tencent": "sh000016", "sina": "sh000016"},
}
# 核心监控指数（快照主展示 + 技术面异动检测对象）
CORE_INDEXES = ["上证指数", "创业板指"]

# 股指期货主力连续（新浪代码 → 对应现货指数名）
FUTURES = {
    "IF": {"sina": "nf_IF0", "index": "沪深300"},
    "IC": {"sina": "nf_IC0", "index": "中证500"},
    "IM": {"sina": "nf_IM0", "index": "中证1000"},
    "IH": {"sina": "nf_IH0", "index": "上证50"},
}

# 汇率（新浪代码 → 显示名）
FX = {
    "fx_susdjpy": "美元/日元",
    "fx_susdcny": "美元/在岸人民币",
}

# 异动阈值（第一版保守值，后续可调）
TH_FX_JPY_PCT = 1.5       # 美元/日元单日涨跌幅绝对值 > 1.5% → 套息平仓风险
TH_VOLUME_RATIO = 1.5     # 成交量 / 5日均量 > 1.5 → 放量
TH_BREAK_WINDOW = 20      # 突破窗口：20日新高/新低
TH_BASIS_HISTORY = 20     # 贴水"走扩"历史窗口：当前基差率创近20日最深才告警（相对分位，防常态贴水误报）
TH_COOLDOWN_HOURS = 6     # 同一因子告警冷却时长（小时）

STATE_PATH = PROJECT_ROOT / "logs" / "factor_state.json"

# ============================================================
# 数据源层（HTTP 适配，均带超时与异常隔离）
# ============================================================
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


def _http_get(url: str, params: dict = None, headers: dict = None, encoding: str = None) -> str:
    """GET 并返回文本；异常统一返回空串（上层判空）"""
    try:
        r = requests.get(url, params=params, headers=headers or _HEADERS, timeout=10)
        if encoding:
            r.encoding = encoding
        return r.text or ""
    except Exception as e:
        logger.warning(f"请求失败 {url}: {e}")
        return ""


def fetch_index_quotes() -> dict:
    """腾讯指数实时行情 → {指数名: {price, prev_close, change_pct, amount_wan}}"""
    result = {}
    codes = ",".join(ix["tencent"] for ix in INDEXES.values())
    text = _http_get(f"http://qt.gtimg.cn/q={codes}", encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split("~")
        if len(parts) < 38:
            continue
        name = parts[1]
        try:
            result[name] = {
                "price": float(parts[3]),
                "prev_close": float(parts[4]),
                "change_pct": float(parts[32]),
                "amount_wan": float(parts[37]),  # 成交额（万元）
            }
        except (ValueError, IndexError):
            continue
    return result


def fetch_index_kline(symbol: str, lmt: int = 65) -> list:
    """新浪指数日K → [{date, open, close, high, low, volume}]，升序

    volume 单位为"股"（上证指数成交量 = 东财手数 × 100），用于量比计算（比例无关单位）。
    """
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(lmt)}
    text = _http_get(
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params=params,
        headers={"Referer": "http://finance.sina.com.cn", **_HEADERS},
    )
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            out.append({
                "date": item["day"],
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "volume": float(item["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_fx() -> dict:
    """新浪外汇 → {符号: {name, price, change_pct}}"""
    result = {}
    codes = ",".join(FX.keys())
    text = _http_get("http://hq.sinajs.cn/list=" + codes, headers={"Referer": "http://finance.sina.com.cn", **_HEADERS}, encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split(",")
        if len(parts) < 12:
            continue
        sym = var.replace("var hq_str_", "").strip()
        try:
            result[sym] = {
                "name": parts[9],
                "price": float(parts[1]),
                "change_pct": float(parts[11]),
            }
        except (ValueError, IndexError):
            continue
    return result


def fetch_index_futures() -> dict:
    """新浪股指期货主力连续 → {期货代码: {price, prev_settle}}"""
    result = {}
    codes = ",".join(f["sina"] for f in FUTURES.values())
    text = _http_get("http://hq.sinajs.cn/list=" + codes, headers={"Referer": "http://finance.sina.com.cn", **_HEADERS}, encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split(",")
        if len(parts) < 5:
            continue
        sym = var.replace("var hq_str_", "").strip()
        for code, conf in FUTURES.items():
            if conf["sina"] == sym:
                try:
                    result[code] = {
                        "price": float(parts[3]),       # 最新价
                        "prev_settle": float(parts[0]),  # 昨结算
                    }
                except (ValueError, IndexError):
                    continue
    return result


# ============================================================
# 因子计算层
# ============================================================
def _third_friday(year: int, month: int) -> date:
    """某年某月的第三个周五（股指期货交割日 = 交割月第三个周五）"""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _next_expiry_days(today: date = None) -> int:
    """主力合约剩余期限估算：距"下月第三个周五"的天数

    股指期货主力合约在当月交割前一周左右切换（如 8/21 交割，8 月中旬主力即切 9 月），
    临近交割的当月合约基差将在交割日收敛，不具对冲成本代表性。
    用下月交割日（当月+1 的第三个周五）近似主力剩余期限，年化贴水率更贴近中性策略实际口径。
    """
    today = today or date.today()
    m = today.month + 1
    y = today.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = _third_friday(y, m)
    return max(1, (d - today).days)


def _ma(values: list, n: int) -> float:
    if len(values) < n or n <= 0:
        return 0.0
    return sum(values[-n:]) / n


def calc_tech_factors(name: str, klines: list, quote: dict) -> dict:
    """技术面因子（指数级）：均线、动量、突破、放量"""
    if not klines or not quote:
        return {"name": name, "available": False}
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    last = klines[-1]
    # 均线
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    price = quote["price"]
    # 均线状态
    if ma5 and ma10 and ma20 and ma60:
        if price > ma5 > ma10 > ma20 > ma60:
            trend = "多头排列"
        elif price < ma5 < ma10 < ma20 < ma60:
            trend = "空头排列"
        else:
            trend = "均线纠缠"
    else:
        trend = "数据不足"
    # 动量（5/20 日涨跌幅）
    mom5 = (price / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    mom20 = (price / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
    # 突破：今日最高 vs 前20日（不含今日）最高；今日收盘 vs 前20日最低
    prev_high = max(k["high"] for k in klines[-TH_BREAK_WINDOW - 1:-1]) if len(klines) > TH_BREAK_WINDOW else last["high"]
    prev_low = min(k["low"] for k in klines[-TH_BREAK_WINDOW - 1:-1]) if len(klines) > TH_BREAK_WINDOW else last["low"]
    breakout = last["high"] > prev_high
    breakdown = last["close"] < prev_low
    # 放量：今日成交量 / 前5日、前20日均量（不含今日，标准量比口径）
    vol5 = _ma(volumes[:-1], 5)
    vol20 = _ma(volumes[:-1], 20)
    ratio5 = last["volume"] / vol5 if vol5 else 0.0
    ratio20 = last["volume"] / vol20 if vol20 else 0.0
    return {
        "name": name, "available": True,
        "price": price, "change_pct": quote["change_pct"],
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "trend": trend,
        "mom5": round(mom5, 2), "mom20": round(mom20, 2),
        "breakout": breakout, "breakdown": breakdown,
        "vol_ratio5": round(ratio5, 2), "vol_ratio20": round(ratio20, 2),
    }


def calc_basis(futures: dict, quotes: dict, remaining_days: int = None) -> dict:
    """股指期货基差：期货价 - 现货指数价；基差率 = 基差/现货×100%；年化贴水率 = 基差率×365/剩余天数

    年化贴水率是中性策略对冲成本的可比口径（日度基差率 -0.8% 对应年化约 -10%~-16%），
    剩余天数按最近交割日（当月/下月第三个周五）估算。
    """
    remaining_days = remaining_days or _next_expiry_days()
    result = {}
    for code, conf in FUTURES.items():
        fut = futures.get(code)
        idx = quotes.get(conf["index"])
        if not fut or not idx:
            continue
        basis = fut["price"] - idx["price"]
        basis_pct = basis / idx["price"] * 100 if idx["price"] else 0.0
        annual_pct = basis_pct * 365 / remaining_days if remaining_days else 0.0
        result[code] = {
            "index": conf["index"],
            "fut": fut["price"], "spot": idx["price"],
            "basis": round(basis, 2), "basis_pct": round(basis_pct, 3),
            "annual_pct": round(annual_pct, 2), "remaining_days": remaining_days,
        }
    return result


# ============================================================
# 异动检测层
# ============================================================
def detect_anomalies(tech: dict, basis: dict, fx: dict, history: dict = None) -> tuple:
    """返回 (signals, new_history)。

    贴水"走扩"用 20 日历史分位：当前基差率创近 20 日最深（且序列≥5 个样本）才告警，
    避免常态贴水（A股股指期货常态日度 -0.8%~-1.3%）误报；其余因子用绝对阈值。
    """
    signals = []
    history = history or {}
    new_history = {k: list(v) for k, v in history.items()}
    # 1) 股指期货贴水走扩（中性策略对冲成本上升，量化倾向降仓）
    for code in ("IC", "IM", "IF", "IH"):
        b = basis.get(code)
        if not b:
            continue
        cur = b["basis_pct"]
        seq = new_history.setdefault(code, [])
        seq.append(cur)
        del seq[:-TH_BASIS_HISTORY]  # 只保留最近 N 个
        if len(seq) >= 5 and cur < min(seq[:-1]):
            prev_min = min(seq[:-1])
            signals.append({
                "key": f"basis_{code}",
                "level": "warning",
                "direction": "bearish",
                "title": f"{code} 贴水走扩（创20日最深）",
                "detail": f"{b['index']}：{code} 基差率 {cur}%（20日最深前值 {prev_min}%，年化 {b['annual_pct']}%），中性策略对冲成本上升、量化倾向降仓",
            })
    # 2) 美元/日元异动（套息交易平仓风险）
    jpy = fx.get("fx_susdjpy")
    if jpy and abs(jpy["change_pct"]) >= TH_FX_JPY_PCT:
        direction = "bearish" if jpy["change_pct"] < 0 else "bullish"
        signals.append({
            "key": "fx_usdjpy",
            "level": "warning",
            "direction": direction,
            "title": "日元急" + ("升" if jpy["change_pct"] < 0 else "贬"),
            "detail": f"美元/日元 {jpy['price']:.2f}（{jpy['change_pct']:+.2f}%），套息交易平仓风险" if jpy["change_pct"] < 0 else f"美元/日元 {jpy['price']:.2f}（{jpy['change_pct']:+.2f}%），日元走弱、套息资金回流风险资产",
        })
    # 3) 技术面：核心指数放量突破 / 放量破位
    for name in CORE_INDEXES:
        t = tech.get(name)
        if not t or not t.get("available"):
            continue
        if t["breakout"] and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            signals.append({
                "key": f"breakout_{name}",
                "level": "info",
                "direction": "bullish",
                "title": f"{name} 放量突破20日新高",
                "detail": f"{name} {t['price']:.2f}（{t['change_pct']:+.2f}%），成交量 {t['vol_ratio5']}x 5日均量",
            })
        if t["breakdown"] and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            signals.append({
                "key": f"breakdown_{name}",
                "level": "warning",
                "direction": "bearish",
                "title": f"{name} 放量跌破20日低点",
                "detail": f"{name} {t['price']:.2f}（{t['change_pct']:+.2f}%），成交量 {t['vol_ratio5']}x 5日均量",
            })
    return signals, new_history


def calc_risk_state(signals: list) -> str:
    """综合异动信号 → 风险状态：任一 warning 信号（贴水走扩/日元急升/放量破位）→ risk_off

    risk_off（风险收缩期）供 real_time_push 联动：对无硬事件佐证的科技利好降级不推。
    """
    if any(s.get("level") == "warning" for s in signals):
        return "risk_off"
    return "neutral"


# ============================================================
# 状态持久化 + 冷却时间去重（因子是时序，非事件指纹）
# 云端（GIST_TOKEN/GIST_ID 存在时）持久化到 Gist 的 factor_state.json，
# 与 real_time_push 的 real_time_state.json 并列——云端 Actions 每次全新容器，
# 本地 logs/factor_state.json 不持久，贴水基线与 risk_state 必须存 Gist 才能跨轮积累。
# 本地模式写 logs/factor_state.json（real_time_push 联动读取）。
# ============================================================
FACTOR_STATE_FILENAME = "factor_state.json"


def _gist_load_factor(token: str, gist_id: str) -> dict:
    """从 Gist 读 factor_state.json（带时间戳防 CDN 缓存；首次运行文件不存在 → 空状态）"""
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-factor",
    }
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            fobj = files.get(FACTOR_STATE_FILENAME)
            if fobj is not None:
                return json.loads(fobj.get("content") or "{}")
        except Exception as e:
            last_error = e
            logger.warning(f"Gist 读取第{attempt + 1}次失败: {e}")
        time.sleep(1)
    # 首次运行（文件尚未创建）与读取失败都返回空——factor 状态丢失只影响贴水基线
    # 积累（重新积累即可），不像推送去重基准丢失会造成重复推送，故允许空。
    if last_error:
        logger.warning(f"Gist factor_state.json 读取失败，按空状态处理: {last_error}")
    return {}


def _gist_save_factor(token: str, gist_id: str, state: dict) -> None:
    """写回 Gist factor_state.json（整文件原子替换）"""
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-factor",
    }
    payload = {"files": {FACTOR_STATE_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}}
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < 2:
                logger.warning(f"Gist 写入第{attempt + 1}次失败: {e}, 1s 后重试")
                time.sleep(1)
    raise RuntimeError(f"Gist factor_state.json 写入失败（已重试2次）: {last_error}")


def _load_state() -> dict:
    """加载状态：云端优先 Gist（跨容器持久），本地用文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            return _gist_load_factor(gist_token, gist_id)
        except Exception as e:
            logger.warning(f"Gist 状态加载失败，降级本地: {e}")
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    """保存状态：本地文件总是写（作为日志/降级）；云端同时写 Gist"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            _gist_save_factor(gist_token, gist_id, state)
            logger.info("因子状态已保存到 Gist（basis_history/risk_state/cooldown）")
        except Exception as e:
            # factor 状态丢失影响小（基线重新积累），本地已保存，不 fail-stop
            logger.warning(f"Gist 因子状态写入失败（本地已保存）: {e}")


def filter_by_cooldown(signals: list, state: dict) -> list:
    """冷却过滤：同一 key 在 TH_COOLDOWN_HOURS 内已告警则跳过"""
    now = time.time()
    fresh = []
    cooldown = state.setdefault("cooldown", {})
    for s in signals:
        last = cooldown.get(s["key"], 0)
        if now - last < TH_COOLDOWN_HOURS * 3600:
            continue
        fresh.append(s)
        cooldown[s["key"]] = now
    return fresh


# ============================================================
# 格式化
# ============================================================
_RED = "#e23a3a"
_GREEN = "#2e7d32"


def format_snapshot(tech: dict, basis: dict, fx: dict) -> str:
    """因子快照（markdown）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## 量化因子快照 {now}", ""]
    # 技术面（指数级）
    lines.append("### 技术面 · 指数级")
    for name in CORE_INDEXES:
        t = tech.get(name)
        if not t or not t.get("available"):
            lines.append(f"- {name}：数据缺失")
            continue
        color = _RED if t["change_pct"] >= 0 else _GREEN
        arrow = "▲" if t["change_pct"] >= 0 else "▼"
        ma_line = f"MA5 {t['ma5']:.0f} / MA20 {t['ma20']:.0f} / MA60 {t['ma60']:.0f}"
        lines.append(
            f"- <font color=\"{color}\">{arrow} {name} {t['price']:.2f}（{t['change_pct']:+.2f}%）</font>"
            f"｜{t['trend']}｜动量 5日{t['mom5']:+.1f}% 20日{t['mom20']:+.1f}%"
        )
        lines.append(f"  > {ma_line}｜量比5日 {t['vol_ratio5']}x" + ("｜⚠️放量" if t["vol_ratio5"] >= TH_VOLUME_RATIO else ""))
    # 宏观流动性
    lines.append("")
    lines.append("### 宏观流动性")
    if basis:
        for code in ("IF", "IC", "IM", "IH"):
            b = basis.get(code)
            if not b:
                continue
            tag = "贴水" if b["basis_pct"] < 0 else "升水"
            lines.append(f"- {code}（{b['index']}）{tag} {abs(b['basis_pct'])}%（年化 {b['annual_pct']}%）｜距交割 {b['remaining_days']} 天")
    if fx:
        for sym, label in FX.items():
            f = fx.get(sym)
            if not f:
                continue
            color = _RED if f["change_pct"] >= 0 else _GREEN
            lines.append(f"- <font color=\"{color}\">{label} {f['price']:.4f}（{f['change_pct']:+.2f}%）</font>")
    return "\n".join(lines)


def format_alert(signal: dict) -> str:
    """单条异动告警"""
    icon = "▲" if signal["direction"] == "bullish" else "▼"
    color = _RED if signal["direction"] == "bullish" else _GREEN
    tag = "强利好" if signal["direction"] == "bullish" else "强利空"
    return f"<font color=\"{color}\">{icon} {tag}</font> **{signal['title']}**\n> {signal['detail']}"


# ============================================================
# 推送出口（复用 push.py）
# ============================================================
def do_push(title: str, content: str) -> dict:
    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    if pushplus_token:
        return push_via_pushplus(pushplus_token, title, content)
    if wecom_webhook:
        return push_via_wecom(wecom_webhook, title, content)
    logger.error("未配置推送后端（PUSHPLUS_TOKEN 或 WECOM_WEBHOOK）")
    return {"code": 400, "msg": "未配置推送后端"}


# ============================================================
# 主流程
# ============================================================
def run_once(push: bool) -> dict:
    quotes = fetch_index_quotes()
    fx = fetch_fx()
    futures = fetch_index_futures()

    tech = {}
    for name in CORE_INDEXES:
        kline = fetch_index_kline(INDEXES[name]["sina"])
        tech[name] = calc_tech_factors(name, kline, quotes.get(name, {}))

    basis = calc_basis(futures, quotes)

    state = _load_state()
    signals, new_history = detect_anomalies(tech, basis, fx, state.get("basis_history"))
    fresh = filter_by_cooldown(signals, state) if push else signals
    risk_state = calc_risk_state(signals)

    snapshot = format_snapshot(tech, basis, fx)
    print(snapshot)

    # 异动检测结果（dry-run 也展示，但不推送、不更新状态）
    if signals:
        print("\n[异动检测]")
        for s in signals:
            print(f"  - {s['title']}｜{s['detail']}")
    else:
        print("\n[异动检测] 无")
    print(f"[风险状态] {risk_state}")

    pushed = []
    if push:
        state["basis_history"] = new_history
        state["risk_state"] = risk_state
        if fresh:
            content = "## 量化因子异动告警\n\n" + "\n\n".join(format_alert(s) for s in fresh)
            r = do_push("量化因子异动", content)
            pushed = [s["title"] for s in fresh]
            print(f"\n[推送] {len(fresh)} 条异动：{pushed} → code={r.get('code', r.get('errcode'))}")
        else:
            print("\n[推送] 本轮无异动（或均在冷却期）")
        _save_state(state)

    return {"tech": tech, "basis": basis, "fx": fx, "signals": signals, "pushed": pushed}


def _is_trading_time(now: datetime = None) -> bool:
    """A股交易时段：周一至周五 9:30-11:30 / 13:00-15:00（不含节假日，第一版用 weekday 近似）

    实时因子（基差/汇率/量比）在盘中分分钟变化，交易时段必须高频轮询；
    非交易时段因子静止，降频省资源。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def main():
    parser = argparse.ArgumentParser(description="量化因子采集器")
    parser.add_argument("--dry-run", action="store_true", help="只采集+计算+打印，不推送")
    parser.add_argument("--push", action="store_true", help="打印快照；有异动且过冷却则推送")
    parser.add_argument("--loop", action="store_true", help="常驻轮询")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.loop:
        # 动态轮询：交易时段高频（默认 5 分钟），非交易时段低频（默认 30 分钟）。
        # 实时因子（基差/汇率/量比）盘中分分钟变化，8-13 日元急升引发跳水即 30 分钟内事件，
        # 30 分钟一轮会滞后错过；盘后因子静止，降频省资源。
        active_poll = max(30, int(os.getenv("RT_POLL_SECONDS", "300")))
        idle_poll = max(300, int(os.getenv("RT_POLL_IDLE_SECONDS", "1800")))
        logger.info(f"因子采集器常驻运行：交易时段每 {active_poll}s 一轮，非交易时段每 {idle_poll}s 一轮")
        while True:
            try:
                run_once(push=True)
            except Exception as e:
                logger.error(f"轮询异常: {e}")
            trading = _is_trading_time()
            poll = active_poll if trading else idle_poll
            logger.info(f"下一轮 {poll}s 后（{'交易时段' if trading else '非交易时段'}）")
            time.sleep(poll)
    else:
        run_once(push=args.push)


if __name__ == "__main__":
    main()
