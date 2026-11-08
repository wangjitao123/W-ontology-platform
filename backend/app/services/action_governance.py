"""动作治理（Action Governance）—— 执行前的三道闸。

现状问题（改造前）：
    `POST /actions/{id}/execute` 直接把 `params` 透给执行器：
      - `parameters_json` 只是装饰，从不校验；必填项缺失也能"成功"
      - `preconditions_json` 从未被求值（列存在、schema 里有、没人用）
      - 写回没有白名单，`modify_attribute` 想改哪个属性就改哪个

三道闸：
    1. **参数校验** —— 按 `parameters_json` 校验必填/类型，套用默认值
    2. **前置条件** —— 按 `preconditions_json` 在目标实例属性上求值
    3. **写回白名单** —— 只能写 `effects_json` 声明过的属性

设计取舍：
    - 前置条件用**结构化条件**（field/operator/value）而不是字符串表达式。
      求值可控、无 `eval` 风险，且与 `business_rules.conditions_json` 风格一致。
    - 比较语义是**严格的**：属性值一律按字符串比较，只有数值运算符才尝试转 float。
      不复用 rule_engine._compare，因为它的 _normalize 会把 "1"/"true" 都当成 True，
      对"全是字符串"的对象属性会产生意外相等。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.rule import EntityAction

# 允许的操作符
OPERATORS = {
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in", "contains", "exists", "not_exists",
}

# 允许写回的动作类型（其余类型即使声明了 effects 也不做写回校验）
WRITEBACK_ACTION_TYPES = {"modify_attribute", "sql_exec"}


class ActionBlocked(Exception):
    """动作被治理规则拦下。reason 是机器可读的原因码。"""

    def __init__(self, reason: str, message: str, extra: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.message = message
        self.extra = extra or {}
        super().__init__(f"{reason}: {message}")

    def to_detail(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, **self.extra}


# ── 比较 ────────────────────────────────────────────────────────

def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare(actual: Any, operator: str, expected: Any) -> bool:
    """严格比较：数值运算符走 float，其余走字符串。"""
    op = operator.strip().lower() if operator else ""

    if op == "exists":
        return actual is not None and str(actual) != ""
    if op == "not_exists":
        return actual is None or str(actual) == ""

    if op in ("==", "!="):
        # 两侧都按字符串比，但对 None 特判，避免 "None" 与 None 混同
        same = (actual is None and expected is None) or (
            actual is not None and expected is not None and str(actual) == str(expected)
        )
        return same if op == "==" else not same

    if actual is None or expected is None:
        return False

    if op in (">", ">=", "<", "<="):
        a, b = _to_float(actual), _to_float(expected)
        if a is None or b is None:
            # 非数值就走字典序（ISO 日期字符串可以这么比）
            a, b = str(actual), str(expected)
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<":
            return a < b
        return a <= b

    if op in ("in", "not_in"):
        pool = expected if isinstance(expected, (list, tuple, set)) else [expected]
        hit = any(str(actual) == str(x) for x in pool)
        return hit if op == "in" else not hit

    if op == "contains":
        return str(expected).lower() in str(actual).lower()

    raise ActionBlocked("bad_operator", f"不支持的操作符: {operator}")


# ── 参数校验 ────────────────────────────────────────────────────

def _coerce_param(name: str, value: Any, declared_type: str | None) -> Any:
    t = (declared_type or "string").strip().lower()
    if t in ("number", "int", "integer", "float", "decimal"):
        f = _to_float(value)
        if f is None:
            raise ActionBlocked("invalid_param", f"参数 {name} 需要数值，收到 {value!r}")
        return f
    if t in ("boolean", "bool"):
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "y"):
            return True
        if low in ("false", "0", "no", "n"):
            return False
        raise ActionBlocked("invalid_param", f"参数 {name} 需要布尔值，收到 {value!r}")
    if t in ("date", "datetime"):
        return str(value)
    return str(value) if value is not None else None


def validate_params(action: EntityAction, params: dict[str, Any]) -> dict[str, Any]:
    """按 parameters_json 校验并规范化参数；不合规抛 ActionBlocked。"""
    defs = action.parameters_json or []
    given = dict(params or {})
    normalized: dict[str, Any] = {}
    missing: list[str] = []

    for d in defs:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not name:
            continue
        if name in given and given[name] is not None:
            normalized[name] = _coerce_param(name, given[name], d.get("type"))
        elif d.get("default_value") is not None:
            normalized[name] = _coerce_param(name, d["default_value"], d.get("type"))
        elif d.get("required", True):
            missing.append(name)

    if missing:
        raise ActionBlocked(
            "missing_params",
            f"缺少必填参数: {', '.join(missing)}",
            {"missing": missing},
        )

    # 未在 schema 里声明的参数透传（执行器可能用到），但不做类型约束
    for k, v in given.items():
        normalized.setdefault(k, v)

    return normalized


# ── 前置条件 ────────────────────────────────────────────────────

def _target_props(
    db: Session, action: EntityAction, object_key: str | None,
) -> dict[str, Any]:
    """取目标实例的属性；前置条件与写回校验都在它上面求值。"""
    from app.services.data_plane.object_index_service import ObjectIndexService

    if not action.entity_id:
        return {}
    if not object_key:
        raise ActionBlocked(
            "object_required",
            "该动作声明了前置条件/写回约束，必须指定 object_key（目标实例主键）",
        )
    props = ObjectIndexService(db).get_props(action.entity_id, object_key)
    if props is None:
        raise ActionBlocked(
            "object_not_found",
            f"目标实例不存在: {action.entity_id}:{object_key}",
            {"object_key": object_key},
        )
    return props


def evaluate_preconditions(
    action: EntityAction, props: dict[str, Any]
) -> list[dict[str, Any]]:
    """逐条求值；返回每条的结果（含失败原因），由调用方决定是否拦截。"""
    results: list[dict[str, Any]] = []
    for cond in action.preconditions_json or []:
        if not isinstance(cond, dict):
            continue
        field = cond.get("field")
        operator = cond.get("operator")
        expected = cond.get("value")
        if not field or not operator:
            results.append({
                "field": field, "operator": operator, "passed": False,
                "reason": "条件定义不完整",
            })
            continue
        actual = props.get(field)
        try:
            passed = compare(actual, operator, expected)
        except ActionBlocked as e:
            results.append({
                "field": field, "operator": operator, "expected": expected,
                "actual": actual, "passed": False, "reason": e.message,
            })
            continue
        results.append({
            "field": field, "operator": operator, "expected": expected,
            "actual": actual, "passed": passed,
        })
    return results


# ── 写回白名单 ──────────────────────────────────────────────────

def declared_effects(action: EntityAction) -> list[dict[str, Any]]:
    return [e for e in (action.effects_json or []) if isinstance(e, dict)]


def allowed_writeback(action: EntityAction) -> set[str]:
    """effects_json 里声明过的可写属性集合。"""
    return {
        str(e["property"]) for e in declared_effects(action)
        if e.get("property")
    }


def check_writeback(action: EntityAction) -> None:
    """写回类动作必须有 effects 声明；否则视为未授权写回。"""
    if action.action_type not in WRITEBACK_ACTION_TYPES:
        return
    if not allowed_writeback(action):
        raise ActionBlocked(
            "no_writeback_declared",
            f"{action.action_type} 类型动作必须用 effects_json 声明可写属性",
        )


def check_target_attribute(action: EntityAction, attribute: str | None) -> None:
    """modify_attribute 只能改白名单内的属性。"""
    if action.action_type != "modify_attribute":
        return
    allowed = allowed_writeback(action)
    if not attribute:
        raise ActionBlocked("invalid_config", "modify_attribute 缺少 target_attribute")
    if allowed and attribute not in allowed:
        raise ActionBlocked(
            "writeback_denied",
            f"属性 {attribute} 不在写回白名单内（允许: {sorted(allowed)}）",
            {"attribute": attribute, "allowed": sorted(allowed)},
        )


# ── 编排 ────────────────────────────────────────────────────────

def govern(
    db: Session,
    action: EntityAction,
    params: dict[str, Any] | None,
    object_key: str | None,
) -> dict[str, Any]:
    """执行前三道闸。通过则返回治理报告，否则抛 ActionBlocked。"""
    if action.status != "active":
        raise ActionBlocked("inactive", "动作未激活")

    normalized = validate_params(action, params or {})

    needs_object = bool(action.preconditions_json) or action.action_type in WRITEBACK_ACTION_TYPES
    props: dict[str, Any] = {}
    pre_results: list[dict[str, Any]] = []

    if action.preconditions_json:
        props = _target_props(db, action, object_key)
        pre_results = evaluate_preconditions(action, props)
        failed = [r for r in pre_results if not r.get("passed")]
        if failed:
            first = failed[0]
            raise ActionBlocked(
                "precondition_failed",
                f"前置条件未满足: {first.get('field')} {first.get('operator')} "
                f"{first.get('expected')!r}（实际 {first.get('actual')!r}）",
                {"failed": failed, "objectKey": object_key},
            )

    check_writeback(action)
    if action.action_type == "modify_attribute":
        check_target_attribute(action, (action.type_config or {}).get("target_attribute"))

    return {
        "ok": True,
        "actionId": action.id,
        "actionType": action.action_type,
        "params": normalized,
        "objectKey": object_key,
        "objectRequired": needs_object,
        "preconditions": pre_results,
        "writebackAllowed": sorted(allowed_writeback(action)),
    }
