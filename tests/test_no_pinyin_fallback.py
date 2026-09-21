# -*- coding: utf-8 -*-
"""pypinyin 缺失时的降级路径测试。

`pypinyin` 是可选依赖，但它对**结果正确性**有影响，不只是"加分项"：

  错字纠错靠"编辑距离 1 + 读音验证"两道闸。以「东北省」为例，它与真实存在的
  「河北省」只差一个字，**只有读音能区分**（dōngběi ≠ héběi）。
  若读音验证因依赖缺失被跳过，这道闸就只剩一半，「东北省」会被当成「河北省」的错写采纳。

该缺陷在**装有 pypinyin 的开发环境不可见**（读音闸正常工作），只在用户实际部署的
最小环境里出现——因此必须用降级模拟来守护，而不能靠普通回归用例。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import engine  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def degraded(monkeypatch):
    """模拟未安装 pypinyin 的环境。"""
    monkeypatch.setattr(engine, "_HAS_PY", False)
    yield


def test_fuzzy_match_rejected_when_degraded(degraded):
    """降级时不得采纳模糊匹配——宁可漏纠，不可把不存在的区划说成存在。"""
    r = engine._resolve_core("东北省")
    assert r["status"] == "unresolvable"
    assert r["result"] is None


def test_generic_suffix_also_rejected_when_degraded(degraded):
    """「方位/大区 + 省」形态一律拒绝。"""
    for q in ("东北省", "华东省", "华北省", "华南省", "西北省", "西南省"):
        r = engine._resolve_core(q)
        assert r["status"] == "unresolvable", f"{q} 应被拒绝，实得 {r['status']}"


def test_main_path_unaffected_when_degraded(degraded):
    """降级只关闭纠错，不得影响主路径（规则 + 词典）。"""
    r = engine._resolve_core("浙江省东阳市横店镇")
    assert r["result"] == "浙江省-金华市-东阳市-横店镇"

    assert engine._resolve_core("襄樊市")["status"] == "resolve"
    assert engine.resolve_by_code("432221")["status"] == "historical"


def test_real_place_not_harmed_when_degraded(degraded):
    """方位词开头的真实地名不得被泛称规则误伤。"""
    r = engine._resolve_core("西南镇")
    assert r["status"] == "resolve"
    assert "陆丰市" in (r["result"] or "")
