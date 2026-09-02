# -*- coding: utf-8 -*-
"""创业板盘中快照的统一格式与数据质量校验。

盘中信号最容易被误读的地方是：日线末根日期等于今天，并不等于它一定是
可审计的盘中快照。这里把价格/成交额、日期、来源、采集时间收敛成一个小
协议，入口、影子记录和回放回测共用同一套校验。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

BJT = timezone(timedelta(hours=8))
AMOUNT_UNITS = frozenset(("shares", "hands", "yuan"))


def _date_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.astimezone(BJT).strftime("%Y-%m-%d") \
            if value.tzinfo else value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if value is None:
        return None
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _captured_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(BJT)
    return parsed.strftime("%Y-%m-%d")


def _finite_number(snapshot: Mapping[str, Any], key: str):
    value = snapshot.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def validate_snapshot(snapshot: Mapping[str, Any],
                      expected_date: Any = None,
                      require_provenance: bool = True) -> dict:
    """校验一条盘中快照，返回 ``{ok, reason, date, source}``。

    ``reason`` 是稳定的短错误码串，供日志、严格门禁和回测报错使用；不把
    无效输入悄悄转成 0，避免“缺数据”和“真实中性”混在一起。
    """
    if not isinstance(snapshot, Mapping):
        return {"ok": False, "reason": "snapshot_not_object",
                "date": None, "source": None}

    issues = []
    day = _date_text(snapshot.get("date"))
    expected = _date_text(expected_date)
    if day is None:
        issues.append("date")
    if expected is not None and day != expected:
        issues.append("date_mismatch")

    close = _finite_number(snapshot, "close")
    amount = _finite_number(snapshot, "amount")
    high = _finite_number(snapshot, "high")
    low = _finite_number(snapshot, "low")
    if close is None or close <= 0:
        issues.append("close")
    if amount is None or amount < 0:
        issues.append("amount")
    if high is None or high <= 0:
        issues.append("high")
    if low is None or low <= 0:
        issues.append("low")
    if high is not None and low is not None and high < low:
        issues.append("high_low_order")
    if high is not None and close is not None and high < close:
        issues.append("high_below_close")
    if low is not None and close is not None and low > close:
        issues.append("low_above_close")

    source = str(snapshot.get("source") or "").strip()
    amount_unit = str(snapshot.get("amount_unit") or "").strip().lower()
    captured_at = snapshot.get("captured_at")
    if require_provenance and not source:
        issues.append("source")
    if require_provenance and amount_unit not in AMOUNT_UNITS:
        issues.append("amount_unit")
    if require_provenance and not str(captured_at or "").strip():
        issues.append("captured_at")
    if captured_at and day is not None and _captured_date(captured_at) != day:
        issues.append("captured_at_date")

    # 保持错误码去重，保证报告不会因为一个条件重复刷屏。
    reason = "ok" if not issues else ",".join(dict.fromkeys(issues))
    return {"ok": not issues, "reason": reason, "date": day,
            "source": source or None}


def make_snapshot_record(date: Any, close: Any, amount: Any,
                         high: Any, low: Any, source: str,
                         captured_at: Any = None,
                         capture_time_type: str = "local_request_time",
                         amount_unit: str = "unknown") -> dict:
    """生成可写入状态或 JSON 归档的规范快照记录。"""
    day = _date_text(date)
    if isinstance(captured_at, datetime):
        parsed = captured_at
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BJT)
        captured_text = parsed.astimezone(BJT).isoformat(timespec="seconds")
    elif captured_at is None:
        captured_text = datetime.now(BJT).isoformat(timespec="seconds")
    else:
        captured_text = str(captured_at).strip()
    return {
        "date": day,
        "close": close,
        "amount": amount,
        "high": high,
        "low": low,
        "source": str(source or "").strip(),
        "captured_at": captured_text,
        "capture_time_type": capture_time_type,
        "amount_unit": str(amount_unit or "unknown").strip().lower(),
    }


def snapshot_time(snapshot: Mapping[str, Any]) -> str:
    """返回用于报告的 ``HH:MM``，解析失败时返回空串。"""
    text = str(snapshot.get("captured_at") or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return ""
    if parsed.tzinfo:
        parsed = parsed.astimezone(BJT)
    return parsed.strftime("%H:%M")
