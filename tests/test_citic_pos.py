# -*- coding: utf-8 -*-
"""push_citic_futures_pos 修复回归测试（不触网）"""
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import push_citic_futures_pos as cpos  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


_REAL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<positionRank>\n"
    "  <data>\n"
    "    <datatypeid>1</datatypeid>\n"
    "    <shortname>中信期货</shortname>\n"
    "    <varvolume>1234</varvolume>\n"
    "  </data>\n"
    "  <data>\n"
    "    <datatypeid>1</datatypeid>\n"
    "    <shortname>GK期货</shortname>\n"
    "    <varvolume>2345</varvolume>\n"
    "  </data>\n"
    "  <data>\n"
    "    <datatypeid>1</datatypeid>\n"
    "    <shortname>YX期货</shortname>\n"
    "    <varvolume>3456</varvolume>\n"
    "  </data>\n"
    "  <data>\n"
    "    <datatypeid>1</datatypeid>\n"
    "    <shortname>ZY期货</shortname>\n"
    "    <varvolume>4567</varvolume>\n"
    "  </data>\n"
    "  <data>\n"
    "    <datatypeid>1</datatypeid>\n"
    "    <shortname>ZZ期货</shortname>\n"
    "    <varvolume>5678</varvolume>\n"
    "  </data>\n"
    "</positionRank>\n"
)
assert len(_REAL_XML) > 500, "样本长度需超过 fetch_product 的 500 字节阈值"

_COMPLETE_XML = _REAL_XML.replace(
    "</positionRank>",
    "  <data>\n"
    "    <datatypeid>2</datatypeid>\n"
    "    <shortname>中信期货</shortname>\n"
    "    <varvolume>234</varvolume>\n"
    "  </data>\n"
    "</positionRank>",
)

_HTML_404 = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"'
    ' "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><body>\n'
    "网页错误&nbsp;&nbsp;入场排名数据未公布\n"
    "</body></html>"
)


def test_fetch_product_rejects_html_error_page(monkeypatch):
    """中金所未公布时返回 200+HTML 错误页 → 判为未就绪 None（修复退出码2重试）"""
    monkeypatch.setattr(cpos.requests, "get", lambda *a, **k: _FakeResp(_HTML_404))
    assert cpos.fetch_product("IF", __import__("datetime").date(2026, 8, 25)) is None


def test_fetch_product_rejects_short_body(monkeypatch):
    """过短响应(<500)视为未就绪"""
    monkeypatch.setattr(cpos.requests, "get", lambda *a, **k: _FakeResp("<html>x</html>"))
    assert cpos.fetch_product("IF", __import__("datetime").date(2026, 8, 25)) is None


def test_fetch_product_accepts_real_xml(monkeypatch):
    """真实 positionRank XML → 正常返回响应供后续解析"""
    monkeypatch.setattr(cpos.requests, "get", lambda *a, **k: _FakeResp(_REAL_XML))
    r = cpos.fetch_product("IF", __import__("datetime").date(2026, 8, 24))
    assert r is not None
    assert r.text == _REAL_XML


def test_parse_xml_real_structure():
    rows = cpos.parse_xml(_REAL_XML)
    assert len(rows) == 5
    assert rows[0]["name"] == "中信期货"
    assert rows[0]["dtype"] == "1"
    assert rows[0]["var"] == 1234


def test_parse_xml_resolves_html_entity():
    """含 &nbsp; 的文本 → 实体正解后不抛 'undefined entity'（防御层）"""
    xml = '<positionRank><data><datatypeid>1</datatypeid><shortname>中信&nbsp;期货</shortname><varvolume>10</varvolume></data></positionRank>'
    rows = cpos.parse_xml(xml)
    assert rows[0]["name"] == "中信\u00a0期货"
    assert rows[0]["var"] == 10


def test_parse_xml_rejects_truncated_document():
    """截断 XML 不得被当作空的有效持仓数据。"""
    with pytest.raises(ValueError, match="XML"):
        cpos.parse_xml(_REAL_XML[:-10])


def test_parse_xml_rejects_missing_required_field():
    """缺少关键字段的品种响应不得生成伪造的零持仓。"""
    malformed = _REAL_XML.replace(
        "<varvolume>1234</varvolume>", "", 1)

    with pytest.raises(ValueError, match="字段"):
        cpos.parse_xml(malformed)


def test_compute_daily_skips_product_with_unusable_xml(monkeypatch):
    """单个品种 XML 解析失败时，只跳过该品种，不让日报主流程崩溃。"""
    malformed = _REAL_XML[:-10]

    def _fetch(product, _day):
        text = malformed if product == "IF" else _COMPLETE_XML
        return _FakeResp(text)

    monkeypatch.setattr(cpos, "fetch_product", _fetch)

    result = cpos.compute_daily(date(2026, 8, 24))

    assert "IF" not in result
    assert set(result) == set(cpos.PRODUCTS) - {"IF"}


def test_main_refuses_to_push_partial_target_day(monkeypatch):
    """目标日四品种不完整时必须返回重试码，且不能读取状态或推送。"""
    monkeypatch.setattr(cpos, "compute_daily", lambda _day: {
        "IF": {"buy_var": 1, "sell_var": 0, "net_var": 1},
    })
    monkeypatch.setattr(cpos, "_load_state", lambda: pytest.fail(
        "部分品种数据不应进入去重/推送流程"))
    monkeypatch.setattr(sys, "argv", ["push_citic_futures_pos.py",
                                       "--date", "20260827"])

    assert cpos.main() == 2


def test_console_configuration_replaces_unsupported_report_characters(monkeypatch):
    """Windows GBK 控制台输出日报中的 Emoji 时不应崩溃。"""
    calls = {}

    class _Console:
        encoding = "gbk"

        def reconfigure(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(cpos.sys, "stdout", _Console())

    cpos._configure_stdout()

    assert calls == {"errors": "replace"}


def test_load_state_gist_failure_does_not_fallback_to_local(monkeypatch, tmp_path):
    """Gist 读取失败时不得使用本地旧状态，避免日报重复推送或覆盖云端状态。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(cpos, "LOCAL_STATE_PATH", tmp_path / "citic_pos_state.json")
    cpos.LOCAL_STATE_PATH.write_text(
        '{"last_pushed_day": "20260827"}', encoding="utf-8")

    monkeypatch.setattr(cpos.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("network down")
    ))

    with pytest.raises(RuntimeError, match="Gist.*读取失败"):
        cpos._load_state()


def test_save_state_failure_is_reported_to_caller(monkeypatch, tmp_path):
    """Gist 写入失败时返回失败，调用方才能让日报任务失败并重试/告警。"""
    monkeypatch.setenv("GIST_TOKEN", "tok123")
    monkeypatch.setenv("GIST_ID", "gid123")
    monkeypatch.setattr(cpos, "LOCAL_STATE_PATH", tmp_path / "citic_pos_state.json")
    monkeypatch.setattr(cpos.requests, "patch", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("network down")
    ))

    assert cpos._save_state({"last_pushed_day": "20260828"}) is False


def test_load_state_rejects_non_object_local_state(monkeypatch, tmp_path):
    """无 Gist 配置时，本地日报状态根节点不是对象应安全降级为空。"""
    monkeypatch.delenv("GIST_TOKEN", raising=False)
    monkeypatch.delenv("GIST_ID", raising=False)
    monkeypatch.setattr(cpos, "LOCAL_STATE_PATH", tmp_path / "citic_pos_state.json")
    cpos.LOCAL_STATE_PATH.write_text("[]", encoding="utf-8")

    assert cpos._load_state() == {}


def test_is_trading_day_rejects_makeup_saturday():
    """中信日报与 A 股统一：法定补班周六仍不算交易日。"""
    assert not cpos._is_trading_day(date(2026, 10, 10))


def test_resolve_target_day_uses_beijing_date(monkeypatch):
    """UTC 次日凌晨不得把中金所日报目标日误判为前一天。"""
    import datetime as _dt

    instant = _dt.datetime(2026, 8, 27, 16, 30, tzinfo=_dt.timezone.utc)

    class _FakeDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    class _FakeDate(_dt.date):
        @classmethod
        def today(cls):
            return _FakeDate(2026, 8, 27)  # 旧 date.today() 会被强制成 UTC 日

    monkeypatch.setattr(cpos, "datetime", _FakeDateTime)
    monkeypatch.setattr(cpos, "date", _FakeDate)
    def _fetch(product, day):
        return _FakeResp(_REAL_XML) if day == date(2026, 8, 28) else None

    monkeypatch.setattr(cpos, "fetch_product", _fetch)

    assert cpos.resolve_target_day() == date(2026, 8, 28)
