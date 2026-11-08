"""版本快照的结构化 diff 与真实依赖影响面。

## 为什么需要这个模块

原来 `ontology_impact.compute_breaking_changes` 只做两件事：

    · 实体被删除
    · 实体被改名

也就是说「把 `ContractVehicle.actual_amount` 的类型从 number 改成 string」
这种改动**根本检测不到**，impact 接口永远返回空列表。

更根本的问题是：版本只能冻结当前状态，没有任何接口能编辑草稿，
所以"提出一个改动"这件事在数据模型里都表达不了。

本模块补上两块：

    1. `diff_snapshots()` —— 逐层比较两份版本快照（实体 / 属性 / 关系），
       输出结构化的变更清单，属性能细到"哪个字段从什么变成什么"。
    2. `find_dependents()` —— 对每条变更，去平台各张表里找真正会受影响的对象：
       指标绑定、动作、规则、函数、语义约束、血缘、场景、智能体。

## 与 ontology_impact.py 的关系

`ontology_impact.py` 保留（审批时用来标记场景/智能体为 stale）；
本模块是"预览影响"的真实实现，两者共用同一份快照读取逻辑。

## 影响面判定依据（都是真实字段，不是猜的）

    指标绑定   ObjectMetricBinding.object_type_id == 实体 id
    动作       EntityAction.entity_id == 实体 id
    规则       BusinessRule.entity_id == 实体 id；
               属性级变更再看 conditions_json 里的 "实体名.属性名"
    函数       OntologyFunction.entity_id == 实体 id
    语义约束   target_id 指向该实体，或 detector_json 里提到该实体绑定的表
    血缘       LineageEdge 的 (source_kind,target_kind)='object_type' 且 id 命中
    场景       AipScene.ontology_bindings 里出现实体名
    智能体     Agent.entity_ids 里出现实体 id
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import OntologyEntity
from app.models.version import (
    OntologyVersion,
    OntologyVersionAttribute,
    OntologyVersionEntity,
    OntologyVersionRelation,
)

logger = logging.getLogger(__name__)

# 属性上值得关注的字段（改动这些才算"语义变了"）
_ATTR_TRACKED = ("name", "type", "required", "source_table", "source_field", "data_status")
_REL_TRACKED = ("name", "rel_type", "cardinality", "acyclic")


@dataclass
class Change:
    """一条变更。"""

    scope: str                  # entity / attribute / relation
    action: str                 # added / removed / renamed / modified
    entity: str                 # 实体名（中文优先）
    entity_id: str | None = None
    target: str | None = None   # 属性名 / 关系名
    field: str | None = None    # 哪个字段变了
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "scope": self.scope,
            "action": self.action,
            "entity": self.entity,
            "entityId": self.entity_id,
            "target": self.target,
            "field": self.field,
            "before": self.before,
            "after": self.after,
        }
        return {k: v for k, v in out.items() if v is not None}

    @property
    def breaking(self) -> bool:
        """算不算"破坏性"（会打断下游）。

        删除实体 / 删除属性 / 改属性类型 / 改来源字段 会让现有绑定失效；
        纯改名、改描述、改必填 只影响展示，不打断。
        """
        if self.action == "removed":
            return self.scope in ("entity", "attribute")
        if self.scope == "attribute" and self.field in ("type", "source_table", "source_field"):
            return True
        if self.scope == "attribute" and self.field == "name":
            return True  # 规则条件里按 "实体.属性名" 匹配，改名会失配
        return False

    @property
    def label(self) -> str:
        if self.scope == "entity":
            if self.action == "added":
                return f"新增实体 {self.entity}"
            if self.action == "removed":
                return f"删除实体 {self.entity}"
            return f"实体改名 {self.entity} → {self.after}"
        if self.scope == "attribute":
            if self.action == "added":
                return f"{self.entity} 新增属性 {self.target}"
            if self.action == "removed":
                return f"{self.entity} 删除属性 {self.target}"
            return f"{self.entity}.{self.target} 的 {self.field}：{self.before} → {self.after}"
        return f"{self.entity} 的关系 {self.target}：{self.action}"


# ── 快照读取 ──────────────────────────────────────────────

def load_snapshot(db: Session, version_id: str) -> dict:
    """把版本的实体/属性/关系读成可比较的结构。

    **必须直接查表，不能用 `version.entities` 这类关系集合**：
    在同一个请求里刚 add 过实体时，集合是已经加载过的空列表，
    SQLAlchemy 不会把新对象追加进去（`_sync_relations` 就是踩了这个坑，
    导致所有已发布版本的关系快照都是 0 条）。
    """
    entities = db.query(OntologyVersionEntity).filter(
        OntologyVersionEntity.version_id == version_id
    ).all()
    if not entities:
        return {"entities": {}}

    ve_ids = [v.id for v in entities]
    attrs = db.query(OntologyVersionAttribute).filter(
        OntologyVersionAttribute.version_entity_id.in_(ve_ids)
    ).all()
    rels = db.query(OntologyVersionRelation).filter(
        OntologyVersionRelation.version_id == version_id
    ).all()

    attr_by_ve: dict[str, dict[str, dict]] = {}
    for a in attrs:
        key = a.source_attribute_id or a.id
        attr_by_ve.setdefault(a.version_entity_id, {})[key] = {
            "id": a.id,
            "name": a.name,
            "type": a.type,
            "required": bool(a.required),
            "description": a.description or "",
            "source_table": a.source_table,
            "source_field": a.source_field,
            "data_status": a.data_status,
        }

    ve_by_id = {v.id: v for v in entities}
    rel_map: dict[str, dict] = {}
    for r in rels:
        key = r.source_relation_id or r.id
        frm = ve_by_id.get(r.from_version_entity_id)
        to = ve_by_id.get(r.to_version_entity_id)
        rel_map[key] = {
            "id": r.id,
            "name": r.name,
            "rel_type": r.rel_type,
            "cardinality": r.cardinality,
            "acyclic": bool(r.acyclic),
            "from": frm.source_entity_id if frm else None,
            "to": to.source_entity_id if to else None,
            "from_name": frm.name if frm else None,
            "to_name": to.name if to else None,
        }

    out: dict[str, dict] = {}
    for v in entities:
        out[v.source_entity_id] = {
            "id": v.id,
            "source_entity_id": v.source_entity_id,
            "name": v.name,
            "name_cn": v.name_cn,
            "tier": v.tier,
            "attributes": attr_by_ve.get(v.id, {}),
        }
    return {"entities": out, "relations": rel_map}


# ── 结构 diff ─────────────────────────────────────────────

def diff_snapshots(old: dict, new: dict) -> list[Change]:
    """比较两份快照，输出变更清单（old → new）。"""
    changes: list[Change] = []
    old_e = old.get("entities") or {}
    new_e = new.get("entities") or {}

    for eid, o in old_e.items():
        display = o.get("name_cn") or o.get("name") or eid
        n = new_e.get(eid)
        if n is None:
            changes.append(Change("entity", "removed", display, eid))
            continue

        if (o.get("name"), o.get("name_cn")) != (n.get("name"), n.get("name_cn")):
            changes.append(Change(
                "entity", "renamed", display, eid,
                before=f"{o.get('name')}/{o.get('name_cn')}",
                after=f"{n.get('name')}/{n.get('name_cn')}",
            ))

        old_attrs = o.get("attributes") or {}
        new_attrs = n.get("attributes") or {}

        for aid, oa in old_attrs.items():
            na = new_attrs.get(aid)
            if na is None:
                changes.append(Change(
                    "attribute", "removed", display, eid, target=oa["name"],
                    before=oa.get("type"),
                ))
                continue
            for f in _ATTR_TRACKED:
                if oa.get(f) != na.get(f):
                    changes.append(Change(
                        "attribute",
                        "renamed" if f == "name" else "modified",
                        display, eid,
                        target=oa["name"] if f != "name" else oa["name"],
                        field=f,
                        before=oa.get(f),
                        after=na.get(f),
                    ))
        for aid, na in new_attrs.items():
            if aid not in old_attrs:
                changes.append(Change(
                    "attribute", "added", display, eid,
                    target=na["name"], after=na.get("type"),
                ))

    for eid, n in new_e.items():
        if eid not in old_e:
            changes.append(Change(
                "entity", "added", n.get("name_cn") or n.get("name") or eid, eid,
            ))

    # 关系：按 source_relation_id 比
    old_r = old.get("relations") or {}
    new_r = new.get("relations") or {}
    for rid, o in old_r.items():
        nr = new_r.get(rid)
        label = f"{o.get('from_name')}→{o.get('to_name')}"
        if nr is None:
            changes.append(Change("relation", "removed", label, o.get("from"), target=o["name"]))
            continue
        for f in _REL_TRACKED:
            if o.get(f) != nr.get(f):
                changes.append(Change(
                    "relation", "modified", label, o.get("from"), target=o["name"],
                    field=f, before=o.get(f), after=nr.get(f),
                ))
    for rid, nr in new_r.items():
        if rid not in old_r:
            changes.append(Change(
                "relation", "added", f"{nr.get('from_name')}→{nr.get('to_name')}",
                nr.get("from"), target=nr["name"],
            ))

    return changes


# ── 影响面 ────────────────────────────────────────────────

def _entity_tables(db: Session, entity_ids: set[str]) -> set[str]:
    """实体绑定的物理表（用于判断语义约束是否受影响）。"""
    tables: set[str] = set()
    for e in db.query(OntologyEntity).filter(OntologyEntity.id.in_(entity_ids)).all():
        cfg = e.config_json or {}
        for key in ("source_table", "table"):
            if cfg.get(key):
                tables.add(str(cfg[key]).split(".")[-1])
        for a in e.attributes:
            if a.source_table:
                tables.add(a.source_table.split(".")[-1])
    return tables


def find_dependents(db: Session, changes: list[Change]) -> dict[str, list[dict]]:
    """对每条变更找出真正受影响的对象。"""
    from app.models.agent import Agent
    from app.models.lineage_edge import LineageEdge
    from app.models.object_metric_binding import ObjectMetricBinding
    from app.models.rule import BusinessRule, EntityAction
    from app.models.scene import AipScene
    from app.models.semantic import SemanticConstraint
    from app.models.function import OntologyFunction

    changed_entities = {c.entity_id for c in changes if c.entity_id}
    # 属性级变更：记录 (实体 id, 属性名)，用于匹配规则条件里的 "Entity.prop"。
    # 必须用 entity_id（= OntologyEntity.id，如 ContractVehicle），
    # 不能用 c.entity —— 那是中文显示名（"合同车辆"），
    # 拿它去匹配 "ContractVehicle.actual_amount" 永远匹配不上，
    # 结果就是规则永远不出现在影响面里。
    attr_refs = {
        (c.entity_id, c.target) for c in changes
        if c.scope == "attribute" and c.target and c.entity_id
    }

    out: dict[str, list[dict]] = {
        "metricBindings": [], "actions": [], "rules": [], "functions": [],
        "constraints": [], "lineage": [], "scenes": [], "agents": [],
    }
    if not changes:
        return out

    # 1) 指标绑定
    for b in db.query(ObjectMetricBinding).filter(
        ObjectMetricBinding.object_type_id.in_(changed_entities)
    ).all():
        out["metricBindings"].append({
            "id": b.id, "name": b.label, "entityId": b.object_type_id,
            "reason": f"绑定在 {b.object_type_id} 上（cube {b.cube_name}）",
        })

    # 2) 动作
    for a in db.query(EntityAction).filter(EntityAction.entity_id.in_(changed_entities)).all():
        out["actions"].append({
            "id": a.id, "name": a.name, "entityId": a.entity_id,
            "reason": f"动作作用于 {a.entity_id}",
        })

    # 3) 规则（实体级 + 属性级）
    for r in db.query(BusinessRule).filter(BusinessRule.entity_id.in_(changed_entities)).all():
        hit = None
        for cond in (r.conditions_json or []):
            if not isinstance(cond, dict):
                continue
            fld = str(cond.get("field") or "")
            for ent_id, attr_name in attr_refs:
                # 规则条件形如 "ContractVehicle.actual_amount"
                if fld == f"{ent_id}.{attr_name}" or (
                    fld.endswith(f".{attr_name}") and ent_id in fld
                ):
                    hit = fld
                    break
            if hit:
                break
        if attr_refs and not hit:
            continue  # 只有属性变更、且规则没引用那个属性 → 不受影响
        out["rules"].append({
            "id": r.id, "name": r.name, "entityId": r.entity_id,
            "reason": f"条件引用了 {hit}" if hit else f"规则挂在 {r.entity_id} 上",
        })

    # 4) 函数
    for f in db.query(OntologyFunction).filter(
        OntologyFunction.entity_id.in_(changed_entities)
    ).all():
        out["functions"].append({
            "id": f.id, "name": f.name, "entityId": f.entity_id,
            "reason": f"函数挂在 {f.entity_id} 上（{f.logic_type}）",
        })

    # 5) 语义约束：target_id 命中实体，或 detector 提到该实体绑定的表
    tables = _entity_tables(db, changed_entities)
    attr_names = {t for _, t in attr_refs}
    for c in db.query(SemanticConstraint).all():
        reason = None
        tid = str(c.target_id or "")
        if any(eid in tid for eid in changed_entities):
            reason = f"约束目标指向 {tid}"
        elif tables:
            blob = str(c.detector_json or "") + " " + str(c.description or "")
            for t in tables:
                if t and t in blob:
                    reason = f"约束引用了表 {t}"
                    break
        if reason is None and attr_names:
            blob = str(c.detector_json or "") + " " + str(c.description or "")
            for a in attr_names:
                if a and a in blob:
                    reason = f"约束提到了属性 {a}"
                    break
        if reason:
            out["constraints"].append({
                "id": c.id, "name": c.name, "severity": c.severity,
                "enforce": bool(c.enforce), "reason": reason,
            })

    # 6) 血缘
    for e in db.query(LineageEdge).filter(LineageEdge.weight > 0).all():
        hit = (
            (e.source_kind == "object_type" and e.source_id in changed_entities)
            or (e.target_kind == "object_type" and e.target_id in changed_entities)
        )
        if hit:
            other = e.target_id if e.source_kind == "object_type" else e.source_id
            out["lineage"].append({
                "id": e.id, "relation": e.relation, "other": other,
                "reason": f"血缘边 {e.source_kind}:{e.source_id} → {e.target_kind}:{e.target_id}",
            })

    # 7) 场景（按实体名匹配 ontology_bindings）
    names = {c.entity for c in changes}
    for s in db.query(AipScene).filter(AipScene.ontology_bindings.isnot(None)).all():
        bindings = s.ontology_bindings or []
        hit = [b for b in bindings if any(n and n in str(b) for n in names)]
        if hit:
            out["scenes"].append({
                "id": s.id, "name": s.name,
                "reason": f"绑定包含 {hit[:3]}",
            })

    # 8) 智能体
    for a in db.query(Agent).filter(Agent.entity_ids.isnot(None)).all():
        ids = a.entity_ids or []
        hit = [x for x in ids if x in changed_entities]
        if hit:
            out["agents"].append({
                "id": a.id, "name": a.name, "reason": f"引用了实体 {hit[:3]}",
            })

    return out


# ── 对外入口 ──────────────────────────────────────────────

def compare_versions(
    db: Session, new_version_id: str, old_version_id: str | None = None,
) -> dict[str, Any]:
    """比较 `new_version_id` 与某个基线版本（默认当前生效版本）。

    返回 {baseline, changes, breakingCount, dependents, summary}。
    """
    new_v = db.get(OntologyVersion, new_version_id)
    if new_v is None:
        raise ValueError(f"版本不存在: {new_version_id}")

    if old_version_id:
        base = db.get(OntologyVersion, old_version_id)
    else:
        base = db.query(OntologyVersion).filter(
            OntologyVersion.is_active == True,  # noqa: E712
            OntologyVersion.id != new_version_id,
        ).first()

    # 关键：拿"当前生效版本自己"做 diff 时，基线上不存在，
    # 如果退回"空基线"，所有实体都会显示成"新增"——纯误导。
    # 这种情况明确返回"无可比基线"，让前端显示"该版本已生效"。
    no_baseline = base is None and bool(new_v.is_active)

    new_snap = load_snapshot(db, new_v.id)
    old_snap = load_snapshot(db, base.id) if base else {"entities": {}, "relations": {}}
    changes = [] if no_baseline else diff_snapshots(old_snap, new_snap)
    dependents = find_dependents(db, changes)

    breaking = [c for c in changes if c.breaking]

    def _d(c: Change) -> dict[str, Any]:
        # breaking / label 是给前端和影响面判定用的，必须一起返回，
        # 否则 impact 里的 `if c.get("breaking")` 永远是假。
        out = c.to_dict()
        out["breaking"] = c.breaking
        out["label"] = c.label
        return out

    return {
        "version": {
            "id": new_v.id,
            "number": new_v.version_number,
            "name": new_v.name,
            "status": new_v.status,
        },
        "baseline": {
            "id": base.id,
            "number": base.version_number,
            "name": base.name,
        } if base else None,
        "changes": [_d(c) for c in changes],
        "breakingCount": len(breaking),
        "dependents": dependents,
        "noBaseline": no_baseline,
        "note": (
            "该版本已是当前生效版本，没有可比基线"
            if no_baseline else None
        ),
        "summary": {
            "entitiesAdded": sum(1 for c in changes if c.scope == "entity" and c.action == "added"),
            "entitiesRemoved": sum(1 for c in changes if c.scope == "entity" and c.action == "removed"),
            "attributesChanged": sum(1 for c in changes if c.scope == "attribute"),
            "relationsChanged": sum(1 for c in changes if c.scope == "relation"),
            "dependentCount": sum(len(v) for v in dependents.values()),
        },
    }
