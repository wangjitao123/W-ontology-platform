"""按对象实例评估规则（而不是按用户）。

## 为什么要单独一个评估器

原 `RuleEvaluator` 的语义是**用户级**的：`FieldResolver` 会拼
`SELECT <col> FROM <table> WHERE <join_field> = :uid`，也就是
"当前用户的这一行记录"。这适合用户画像类本体（"这个用户的合约是否有效"）。

本项目的本体是**业务对象**：合同、物料、应收未清项、融资合同。
这些对象没有"当前用户"的概念——不存在"admin 的那份合同"。
所以原路径下每条规则的 `actual` 都是 `null`，`triggered` 永远是 false。

（这不是 bug，是设计假设不同。实测：3 条规则都能评估，但条件值全部解析为 null。）

## 这个模块做什么

直接读**对象索引**（Postgres `object_index`），对每条规则的条件
逐个对象实例求值，给出
    total    参与了评估的对象数
    matched  满足条件的对象数
    samples  前几个命中的对象（带主键和实际值）
    errors   无法求值的条件（属性不存在等）

这样"合同金额必须为正"能回答"3,158 份合同里有几份金额异常"——
这才是业务能用的规则结论。

## 边界

· 只支持 `conditions_json` 里的简单比较（`_compare` 支持的算子）
· `type == "function_call"` 的条件在这里跳过（需要额外参数，交给原引擎）
· 采样默认 500 个实例，避免把 28,000 行全拉到 Python 里
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import OntologyEntity
from app.models.rule import BusinessRule
from app.services.rule_engine import _compare

logger = logging.getLogger(__name__)


@dataclass
class ConditionStat:
    field: str
    display: str
    operator: str
    expected: Any
    evaluated: int = 0
    matched: int = 0
    unresolved: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "display": self.display,
            "operator": self.operator,
            "expected": self.expected,
            "evaluated": self.evaluated,
            "matched": self.matched,
            "unresolved": self.unresolved,
            "error": self.error,
        }


@dataclass
class RuleStat:
    rule_id: str | None
    rule_name: str
    entity: str
    total: int = 0
    matched: int = 0
    conditions: list[ConditionStat] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None

    @property
    def triggered(self) -> bool:
        """只要有对象命中就算触发（规则是"发现问题"，不是"必须全部满足"）。"""
        return self.matched > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "ruleName": self.rule_name,
            "entity": self.entity,
            "total": self.total,
            "matched": self.matched,
            "triggered": self.triggered,
            "matchRate": round(self.matched / self.total, 4) if self.total else 0.0,
            "conditions": [c.to_dict() for c in self.conditions],
            "samples": self.samples,
            "note": self.note,
        }


def _prop_name(field_ref: str) -> tuple[str | None, str]:
    """把 "Entity.prop" 拆开。"""
    if "." not in field_ref:
        return None, field_ref
    e, p = field_ref.split(".", 1)
    return e, p


def evaluate_rule(
    db: Session, rule: BusinessRule, *, sample: int = 500,
) -> RuleStat:
    """对一条规则做实例级评估。"""
    from app.services.data_plane.object_index_service import ObjectIndexService

    entity = db.get(OntologyEntity, rule.entity_id) if rule.entity_id else None
    stat = RuleStat(
        rule_id=rule.id,
        rule_name=rule.name,
        entity=entity.id if entity else (rule.entity_id or ""),
    )
    if entity is None:
        stat.note = "规则未关联对象类型"
        return stat

    conds = [c for c in (rule.conditions_json or []) if isinstance(c, dict)]
    if not conds:
        stat.note = "规则没有 conditions_json，无法实例化评估"
        return stat
    if any(c.get("type") == "function_call" for c in conds):
        stat.note = "含 function_call 条件，需原 RuleEvaluator 处理"
        return stat

    svc = ObjectIndexService(db)
    if not svc.has_data():
        stat.note = "对象索引为空，先跑 /studio/index/sync"
        return stat

    # 分页取样本。只取第一页会让"3,159 份合同里有几份超阈值"这种问题
    # 得到完全不具代表性的答案（实测前 200 个里 11 份，全量 32 份）。
    #
    # 注意：object_store 把 page_size 硬限到 200，所以绝不能拿"请求了多少条"
    # 判断是不是最后一页（请求 500 只回 200，会被误判成末尾）。
    # 用响应里的 total 判断。
    items: list[dict] = []
    page = 1
    while len(items) < sample:
        batch = svc.query(entity.id, page=page,
                          page_size=min(200, sample - len(items)), include_props=True)
        chunk = batch.get("items") or []
        if not chunk:
            break
        items.extend(chunk)
        total = int(batch.get("total") or 0)
        if len(items) >= total or len(items) >= sample:
            break
        page += 1
    stat.total = len(items)
    if not items:
        stat.note = "该对象类型没有实例"
        return stat

    # 为每个条件建统计
    parsed: list[tuple[ConditionStat, str]] = []
    for c in conds:
        fld = str(c.get("field") or "")
        _, prop = _prop_name(fld)
        cs = ConditionStat(
            field=fld,
            display=str(c.get("display") or prop),
            operator=str(c.get("operator") or "=="),
            expected=c.get("value"),
        )
        parsed.append((cs, prop))
        stat.conditions.append(cs)

    for it in items:
        props = it.get("properties") or {}
        all_matched = True
        for cs, prop in parsed:
            raw = props.get(prop)
            if raw is None:
                cs.unresolved += 1
                all_matched = False
                continue
            actual = _coerce(raw, cs.expected)
            cs.evaluated += 1
            if _compare(actual, cs.operator, cs.expected):
                cs.matched += 1
            else:
                all_matched = False
        if all_matched:
            stat.matched += 1
            if len(stat.samples) < 5:
                stat.samples.append({
                    "objectKey": it.get("objectKey"),
                    "title": it.get("title"),
                    "properties": {
                        p: props.get(p) for _, p in parsed
                    },
                })

    if any(c.unresolved == stat.total for c in stat.conditions):
        missing = [c.display for c in stat.conditions if c.unresolved == stat.total]
        stat.note = f"这些属性在对象实例里不存在：{missing}"
    return stat


def _coerce(raw: Any, expected: Any) -> Any:
    """对象属性统一存成字符串，比较数值时需要还原类型。"""
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if isinstance(expected, bool):
        low = str(raw).strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        return raw
    return raw


def evaluate_all(
    db: Session, *, sample: int = 500, entity_id: str | None = None,
) -> dict[str, Any]:
    """评估全部启用规则，返回汇总。"""
    q = db.query(BusinessRule).filter(BusinessRule.status == "active")
    if entity_id:
        q = q.filter(BusinessRule.entity_id == entity_id)
    rules = q.all()

    stats = [evaluate_rule(db, r, sample=sample) for r in rules]
    triggered = [s for s in stats if s.triggered]
    return {
        "sampleSize": sample,
        "rules": [s.to_dict() for s in stats],
        "evaluated": len(stats),
        "triggered": len(triggered),
        "violatingObjects": sum(s.matched for s in stats),
    }
