"""动作治理三道闸的测试。

这些函数是纯逻辑，最容易出"能跑但结果是错的"问题，所以优先覆盖：
    1. 比较语义（数值 vs 字符串、空值、集合、坏操作符）
    2. 参数校验（必填缺失、默认值、类型转换、非法值）
    3. 前置条件求值（通过 / 不通过 / 条件定义不完整）
    4. 写回白名单（未声明 / 越权属性）
"""
from types import SimpleNamespace

import pytest

from app.services.action_governance import (
    ActionBlocked,
    allowed_writeback,
    check_target_attribute,
    check_writeback,
    compare,
    evaluate_preconditions,
    validate_params,
)


def _action(**kw) -> SimpleNamespace:
    base = dict(
        id="act_1",
        action_type="modify_attribute",
        status="active",
        entity_id="E1",
        parameters_json=[],
        preconditions_json=[],
        effects_json=[],
        type_config={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── 比较语义 ──────────────────────────────────────────────

@pytest.mark.parametrize("actual,op,expected,ok", [
    ("五星", "==", "五星", True),
    ("五星", "==", "四星", False),
    ("四星", "!=", "五星", True),
    # 对象属性全是字符串，数值运算符要能转成数比
    ("188.50", ">", 100, True),
    ("188.50", ">", "200", False),
    ("188.50", ">=", "188.50", True),
    ("87", "<", "100", True),
    ("2026-07", ">=", "2026-01", True),   # 非数值走字典序，ISO 日期可比
    ("山东", "in", ["山东", "山西"], True),
    ("河北", "in", ["山东", "山西"], False),
    ("河北", "not_in", ["山东", "山西"], True),
    ("整车零部件", "contains", "零部件", True),
    ("", "exists", None, False),
    ("x", "exists", None, True),
    (None, "not_exists", None, True),
])
def test_compare(actual, op, expected, ok):
    assert compare(actual, op, expected) is ok


def test_compare_rejects_unknown_operator():
    with pytest.raises(ActionBlocked) as exc:
        compare("a", "~~", "a")
    assert exc.value.reason == "bad_operator"


# ── 参数校验 ──────────────────────────────────────────────

def test_validate_params_missing_required():
    action = _action(parameters_json=[
        {"name": "target_level", "type": "string", "required": True},
        {"name": "reason", "type": "string", "required": True},
    ])
    with pytest.raises(ActionBlocked) as exc:
        validate_params(action, {"target_level": "四星"})
    assert exc.value.reason == "missing_params"
    assert exc.value.extra["missing"] == ["reason"]


def test_validate_params_applies_default_and_coerces():
    action = _action(parameters_json=[
        {"name": "amount", "type": "number", "required": True},
        {"name": "note", "type": "string", "required": False, "default_value": "无"},
        {"name": "urgent", "type": "boolean", "required": False},
    ])
    out = validate_params(action, {"amount": "1500", "urgent": "yes"})
    assert out["amount"] == 1500.0
    assert out["note"] == "无"
    assert out["urgent"] is True


def test_validate_params_rejects_bad_number():
    action = _action(parameters_json=[
        {"name": "amount", "type": "number", "required": True},
    ])
    with pytest.raises(ActionBlocked) as exc:
        validate_params(action, {"amount": "很多"})
    assert exc.value.reason == "invalid_param"


def test_validate_params_keeps_undeclared_params():
    """未在 schema 里声明的参数透传（执行器可能要用 object_key 之类）。"""
    action = _action(parameters_json=[])
    out = validate_params(action, {"object_key": "C0001"})
    assert out["object_key"] == "C0001"


# ── 前置条件 ──────────────────────────────────────────────

def test_preconditions_pass_and_fail_details():
    action = _action(preconditions_json=[
        {"field": "star_level", "operator": "==", "value": "五星"},
        {"field": "arpu", "operator": ">=", "value": 100},
    ])
    results = evaluate_preconditions(action, {"star_level": "五星", "arpu": "188.50"})
    assert [r["passed"] for r in results] == [True, True]

    failed = evaluate_preconditions(action, {"star_level": "四星", "arpu": "88"})
    assert failed[0]["passed"] is False
    assert failed[0]["actual"] == "四星"
    assert failed[0]["expected"] == "五星"


def test_precondition_with_bad_definition_is_failure_not_crash():
    action = _action(preconditions_json=[{"field": "x"}])
    results = evaluate_preconditions(action, {"x": 1})
    assert results[0]["passed"] is False
    assert "不完整" in results[0]["reason"]


# ── 写回白名单 ────────────────────────────────────────────

def test_allowed_writeback_reads_effects():
    action = _action(effects_json=[
        {"property": "star_level", "operation": "set"},
        {"property": "region", "operation": "set"},
    ])
    assert allowed_writeback(action) == {"star_level", "region"}


def test_writeback_action_without_effects_is_blocked():
    action = _action(action_type="modify_attribute", effects_json=[])
    with pytest.raises(ActionBlocked) as exc:
        check_writeback(action)
    assert exc.value.reason == "no_writeback_declared"


def test_writeback_check_ignores_non_writeback_actions():
    """notification 这类动作不需要 effects 声明。"""
    check_writeback(_action(action_type="notification", effects_json=[]))


def test_target_attribute_must_be_in_whitelist():
    action = _action(
        action_type="modify_attribute",
        effects_json=[{"property": "star_level"}],
    )
    check_target_attribute(action, "star_level")   # 白名单内，放行
    with pytest.raises(ActionBlocked) as exc:
        check_target_attribute(action, "region")
    assert exc.value.reason == "writeback_denied"
