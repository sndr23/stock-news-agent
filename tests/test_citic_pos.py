# -*- coding: utf-8 -*-
"""push_citic_futures_pos 修复回归测试（不触网）"""
import sys
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