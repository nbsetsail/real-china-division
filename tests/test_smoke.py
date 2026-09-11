# -*- coding: utf-8 -*-
"""引擎冒烟测试：CI 与本地回归的最小集（黄金测试集不随开源仓库分发）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from engine import resolve, resolve_by_code  # noqa: E402


def test_resolve_basic():
    r = resolve("杭州市西湖区")
    assert r["status"] == "resolve"
    assert "浙江省" in r["result"] and "西湖区" in r["result"]


def test_historical_name():
    r = resolve("襄樊市樊城区")
    assert r["status"] == "resolve" and "襄阳市" in r["result"]


def test_ambiguous():
    r = resolve("通州区")
    assert r["status"] == "ambiguous" and isinstance(r["result"], list) and len(r["result"]) >= 2


def test_special():
    r = resolve("苏州工业园区")
    assert r["status"] in ("special", "resolve")


def test_unresolvable():
    assert resolve("苏北")["status"] == "unresolvable"


def test_pinyin():
    r = resolve("深镇")
    assert r["status"] == "resolve" and "深圳市" in r["result"]


def test_township():
    r = resolve("下沙街道")
    assert r["status"] == "resolve" and "钱塘区" in r["result"]


def test_code_lookup():
    r = resolve_by_code("110000000000")
    assert r["status"] == "resolve" and r["result"] == "北京市"


def test_historical_code():
    r = resolve_by_code("510124")
    assert r["status"] == "historical" and "郫都区" in r["result"]


def test_bad_code():
    assert resolve_by_code("999999")["status"] == "unresolvable"
