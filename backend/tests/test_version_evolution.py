"""版本演进测试：diff / 影响面 / 生效 / 回滚。

## 为什么这些测试重要

版本系统原来是个摆设，四处断点都在这里钉住：

    1. `diff` 旧实现只认"实体删除/改名" —— 属性类型从 decimal 改成 string
       完全检测不到。`diff_snapshots` 的用例覆盖属性增/删/改/改名。
    2. `impact` 旧实现只查场景和智能体两张表，且智能体表已被清空 → 恒为空。
       这里验证：改 `ContractVehicle.actual_amount` 必须能影响
       「大额合同需人工复核」这条规则（它的条件就是引用这个属性）。
    3. `approve` 旧实现只改状态，实际对象表一个字节没动。
       `apply_snapshot_to_live` 的幂等用例证明"应用当前生效版本"不会产生变更。
    4. `rollback` 旧实现只多一条记录。端到端回滚由
       `scripts/verify_version_lifecycle.py` 覆盖（需要跑起来的服务），
       这里覆盖不依赖网络的纯逻辑部分。

## 两个容易搞混的 id

    OntologyEntity.id        = "ContractVehicle"（字符串主键）
    OntologyVersionEntity.id = uuid（每次快照一份）
    规则条件里的 field        = "ContractVehicle.actual_amount"

匹配必须用前者。用显示名（"合同车辆"）去匹配永远是空——这个坑踩过一次。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.services.version_diff import (
    Change,
    diff_snapshots,
    find_dependents,
    load_snapshot,
)


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


# ── 快照 diff（纯函数，不碰数据库）────────────────────────────

def _snap(entities: dict, relations: dict | None = None) -> dict:
    return {"entities": entities, "relations": relations or {}}


def _ent(name: str, attrs: dict, name_cn: str = "") -> dict:
    return {
        "id": f"ve-{name}",
        "source_entity_id": name,
        "name": name,
        "name_cn": name_cn or name,
        "tier": 1,
        "attributes": attrs,
    }


def _attr(name: str, type_: str = "string", **kw) -> dict:
    base = {
        "id": f"va-{name}", "name": name, "type": type_, "required": False,
        "description": "", "source_table": None, "source_field": None,
        "data_status": "未确认来源",
    }
    base.update(kw)
    return base


def test_diff_detects_attribute_type_change():
    """属性类型变了必须检出——旧实现完全看不到这类改动。"""
    old = _snap({"ContractVehicle": _ent("ContractVehicle", {"a1": _attr("actual_amount", "decimal")})})
    new = _snap({"ContractVehicle": _ent("ContractVehicle", {"a1": _attr("actual_amount", "string")})})
    changes = diff_snapshots(old, new)
    assert len(changes) == 1
    c = changes[0]
    assert c.scope == "attribute"
    assert c.target == "actual_amount"
    assert c.field == "type"
    assert (c.before, c.after) == ("decimal", "string")
    assert c.breaking is True, "改类型会让现有绑定/规则失效，必须算破坏性"


def test_diff_detects_added_and_removed_attribute():
    old = _snap({"E": _ent("E", {"a1": _attr("keep"), "a2": _attr("gone")})})
    new = _snap({"E": _ent("E", {"a1": _attr("keep"), "a3": _attr("fresh")})})
    changes = diff_snapshots(old, new)
    actions = {(c.target, c.action) for c in changes}
    assert ("gone", "removed") in actions
    assert ("fresh", "added") in actions


def test_diff_detects_entity_added_and_removed():
    old = _snap({"A": _ent("A", {})})
    new = _snap({"B": _ent("B", {})})
    changes = diff_snapshots(old, new)
    assert {c.action for c in changes} == {"added", "removed"}


def test_diff_detects_attribute_rename():
    old = _snap({"E": _ent("E", {"a1": _attr("old_name")})})
    new = _snap({"E": _ent("E", {"a1": _attr("new_name")})})
    changes = diff_snapshots(old, new)
    c = next(x for x in changes if x.field == "name")
    assert c.action == "renamed"
    assert c.breaking is True, "规则条件按 实体.属性名 匹配，改名会失配"


def test_diff_no_change_returns_empty():
    s = _snap({"E": _ent("E", {"a1": _attr("x")})})
    assert diff_snapshots(s, s) == []


@pytest.mark.parametrize(
    ("scope", "action", "field", "expected"),
    [
        ("entity", "removed", None, True),
        ("entity", "renamed", None, False),
        ("entity", "added", None, False),
        ("attribute", "removed", None, True),
        ("attribute", "modified", "type", True),
        ("attribute", "modified", "source_field", True),
        ("attribute", "modified", "description", False),
        ("attribute", "modified", "required", False),
        ("relation", "removed", None, False),
    ],
)
def test_breaking_classification(scope, action, field, expected):
    c = Change(scope, action, "E", "E", target="t", field=field)
    assert c.breaking is expected


# ── 影响面（读真实数据库）──────────────────────────────────────

def test_impact_finds_rule_referencing_changed_attribute(db):
    """改 ContractVehicle.actual_amount 必须影响「大额合同需人工复核」。

    这条规则的 conditions_json 就是
    {"field": "ContractVehicle.actual_amount", "operator": ">", "value": 10000000}
    用显示名（"合同车辆"）去匹配会漏掉——这里正是回归点。
    """
    changes = [Change(
        "attribute", "modified", "合同车辆", "ContractVehicle",
        target="actual_amount", field="type", before="decimal", after="string",
    )]
    deps = find_dependents(db, changes)
    names = [r["name"] for r in deps["rules"]]
    assert any("大额合同" in n for n in names), (
        f"规则影响面没有命中，实际拿到 {names}"
    )
    hit = next(r for r in deps["rules"] if "大额合同" in r["name"])
    assert "actual_amount" in hit["reason"]


def test_impact_skips_rules_for_unrelated_attribute(db):
    """改一个规则根本不引用的属性，不该把规则算进来。"""
    changes = [Change(
        "attribute", "modified", "合同车辆", "ContractVehicle",
        target="__no_such_attribute__", field="type", before="a", after="b",
    )]
    deps = find_dependents(db, changes)
    assert deps["rules"] == [], f"不该命中规则，实际 {[r['name'] for r in deps['rules']]}"


def test_impact_finds_binding_action_function(db):
    changes = [Change("entity", "removed", "合同车辆", "ContractVehicle")]
    deps = find_dependents(db, changes)
    assert len(deps["metricBindings"]) > 0, "指标绑定应受影响"
    assert len(deps["actions"]) > 0, "动作应受影响"
    assert len(deps["functions"]) > 0, "函数应受影响"


def test_impact_empty_for_no_changes(db):
    deps = find_dependents(db, [])
    assert all(v == [] for v in deps.values())


def test_impact_lineage_edges_resolve(db):
    changes = [Change("entity", "removed", "合同车辆", "ContractVehicle")]
    deps = find_dependents(db, changes)
    for edge in deps["lineage"]:
        assert edge.get("relation"), f"血缘边缺关系类型: {edge}"


# ── 应用快照：幂等性 ──────────────────────────────────────────

def test_apply_active_version_is_idempotent(db):
    """把"当前生效版本"应用到实际对象，不应该产生任何变更。

    如果不幂等，说明快照和实际对象漂移了——那正是"发布没生效"的症状。
    """
    from app.models.version import OntologyVersion
    from app.services.version_apply import apply_snapshot_to_live

    v = db.query(OntologyVersion).filter(OntologyVersion.is_active == True).first()  # noqa: E712
    if v is None:
        pytest.skip("没有生效版本")

    report = apply_snapshot_to_live(db, v)
    db.rollback()  # 只验证不落库
    total_changes = (
        report["entitiesUpdated"] + report["entitiesCreated"]
        + report["attributesUpdated"] + report["attributesCreated"]
        + report["relationsUpdated"] + report["relationsCreated"]
    )
    assert total_changes == 0, (
        f"生效版本应用后仍有 {total_changes} 处变更，说明快照与实际对象漂移：{report}"
    )


def test_snapshot_has_attributes_and_relations(db):
    """生效版本的快照必须完整——关系快照曾经因 `v.entities` 为陈旧空集合而恒为 0。"""
    from app.models.version import (
        OntologyVersion,
        OntologyVersionAttribute,
        OntologyVersionEntity,
        OntologyVersionRelation,
    )

    v = db.query(OntologyVersion).filter(OntologyVersion.is_active == True).first()  # noqa: E712
    if v is None:
        pytest.skip("没有生效版本")

    snap = load_snapshot(db, v.id)
    assert snap["entities"], "快照里没有实体"
    n_attr = sum(len(e["attributes"]) for e in snap["entities"].values())
    assert n_attr > 0, "快照里没有属性"
    assert snap["relations"], (
        "快照里没有关系——检查 _sync_relations 是否又退回读 v.entities"
    )
    # 与直接 count 对账
    assert len(snap["relations"]) == db.query(OntologyVersionRelation).filter(
        OntologyVersionRelation.version_id == v.id
    ).count()
    assert n_attr == (
        db.query(OntologyVersionAttribute)
        .join(OntologyVersionEntity,
              OntologyVersionAttribute.version_entity_id == OntologyVersionEntity.id)
        .filter(OntologyVersionEntity.version_id == v.id)
        .count()
    )


def test_active_version_has_no_baseline(db):
    """生效版本自己跟自己比没有基线，必须返回 noBaseline 而不是"全部新增"。

    这是给前端用的：如果拿空基线去 diff 一个已生效版本，
    所有实体都会显示成"新增"，纯误导。
    """
    from app.models.version import OntologyVersion
    from app.services.version_diff import compare_versions

    v = db.query(OntologyVersion).filter(OntologyVersion.is_active == True).first()  # noqa: E712
    if v is None:
        pytest.skip("没有生效版本")

    res = compare_versions(db, v.id)
    assert res["noBaseline"] is True
    assert res["changes"] == []
    assert res["note"], "应给出说明文字供前端显示"
