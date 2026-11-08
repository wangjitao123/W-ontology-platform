"""把版本快照应用到实际对象（"生效"）。

## 为什么必须有这一步

原来的 `approve_version` 只做一件事：把 `status` 改成 `published`、
把 `is_active` 置真。**实际对象表（ontology_entities / entity_attributes /
entity_relations）一个字节都没动。**

结果就是：

    · 你在草稿里改了属性类型 → 审批通过 → 实际属性没变
    · "回滚到 v1" → 只是多了一条版本记录 → 实际数据仍是新的

版本系统于是成了纯审计记录，不是"发布"。这里补上应用快照这一步。

## 语义

    approve(普通版本)   → 把该版本快照写回实际对象
    approve(回滚版本)   → 同样，只是快照内容来自被回滚的那个版本
                          （rollback 接口已经把那版内容复制进新版本了）

## 安全边界

**不删除**实际对象里多出来的东西。

如果快照里没有、但实际表里还有（比如上线后有人手工加了个属性），
这里只登记到 `extras` 让人去判断，不自动删——删属性会连带打断
指标绑定、血缘、规则条件，属于不可逆操作，不该由"审批通过"顺手做掉。

## 新增属性

草稿里新增的属性 `source_attribute_id` 为空（它还没有对应的实际属性）。
应用时创建一个新的 `EntityAttribute`，并把新 id 回写到版本属性上，
这样下次 diff 就能按 id 正确比较，而不是每次都显示"新增"。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import EntityAttribute, OntologyEntity
from app.models.relation import EntityRelation
from app.models.version import (
    OntologyVersion,
    OntologyVersionAttribute,
    OntologyVersionEntity,
    OntologyVersionRelation,
)
from app.utils.identifiers import gen_uuid

logger = logging.getLogger(__name__)

# 应用快照时要同步的字段
_ENTITY_FIELDS = ("name", "name_cn", "tier", "description", "config_json", "publish_config")
_ATTR_FIELDS = (
    "name", "type", "description", "required", "example",
    "constraints_json", "source_table", "source_field", "data_status",
)
_REL_FIELDS = ("name", "rel_type", "cardinality", "acyclic", "description")


def apply_snapshot_to_live(db: Session, version: OntologyVersion) -> dict[str, Any]:
    """把版本快照写回实际对象表。返回变更报告。"""
    report: dict[str, Any] = {
        "version": version.version_number,
        "entitiesUpdated": 0,
        "entitiesCreated": 0,
        "attributesUpdated": 0,
        "attributesCreated": 0,
        "relationsUpdated": 0,
        "relationsCreated": 0,
        "extras": {"entities": [], "attributes": [], "relations": []},
        "warnings": [],
    }

    ves = db.query(OntologyVersionEntity).filter(
        OntologyVersionEntity.version_id == version.id
    ).all()
    if not ves:
        report["warnings"].append("该版本没有实体快照，未做任何实际变更")
        return report

    # 版本实体 id → 实际实体 id（关系两端要用）
    ve_to_entity: dict[str, str] = {}

    for ve in ves:
        eid = ve.source_entity_id
        ve_to_entity[ve.id] = eid

        live = db.get(OntologyEntity, eid)
        if live is None:
            # 快照里有、实际表里没有 → 重建（沿用原 id，避免打断所有引用）
            live = OntologyEntity(
                id=eid,
                name=ve.name,
                name_cn=ve.name_cn,
                tier=ve.tier,
                description=ve.description,
                config_json=ve.config_json,
                publish_config=ve.publish_config,
                status="published",
            )
            db.add(live)
            db.flush()
            report["entitiesCreated"] += 1
            report["warnings"].append(f"实体 {eid} 在实际表中不存在，已按快照重建")
        else:
            changed = False
            for f in _ENTITY_FIELDS:
                new_val = getattr(ve, f, None)
                if getattr(live, f) != new_val:
                    setattr(live, f, new_val)
                    changed = True
            if changed:
                report["entitiesUpdated"] += 1

        # ── 属性 ──
        vas = db.query(OntologyVersionAttribute).filter(
            OntologyVersionAttribute.version_entity_id == ve.id
        ).all()
        seen_attr_ids: set[str] = set()

        for va in vas:
            aid = va.source_attribute_id
            if aid:
                seen_attr_ids.add(aid)
                live_attr = db.get(EntityAttribute, aid)
            else:
                live_attr = None

            if live_attr is None:
                new_attr = EntityAttribute(
                    id=aid or gen_uuid(),
                    entity_id=eid,
                    name=va.name,
                    type=va.type,
                    description=va.description or "",
                    required=bool(va.required),
                    example=va.example,
                    constraints_json=va.constraints_json,
                    source_table=va.source_table,
                    source_field=va.source_field,
                    data_status=va.data_status or "未确认来源",
                )
                db.add(new_attr)
                db.flush()
                # 回写 id，让下次 diff 能按 id 对上而不是显示"新增"
                va.source_attribute_id = new_attr.id
                seen_attr_ids.add(new_attr.id)
                report["attributesCreated"] += 1
            else:
                changed = False
                for f in _ATTR_FIELDS:
                    new_val = getattr(va, f)
                    if f == "required":
                        new_val = bool(new_val)
                    if getattr(live_attr, f) != new_val:
                        setattr(live_attr, f, new_val)
                        changed = True
                if changed:
                    report["attributesUpdated"] += 1

        # 实际表里多出来的属性：不删，只登记
        live_attrs = db.query(EntityAttribute).filter(
            EntityAttribute.entity_id == eid
        ).all()
        for la in live_attrs:
            if la.id not in seen_attr_ids:
                report["extras"]["attributes"].append(
                    {"entityId": eid, "id": la.id, "name": la.name}
                )

    # ── 关系 ──
    vrs = db.query(OntologyVersionRelation).filter(
        OntologyVersionRelation.version_id == version.id
    ).all()
    seen_rel_ids: set[str] = set()
    for vr in vrs:
        from_eid = ve_to_entity.get(vr.from_version_entity_id)
        to_eid = ve_to_entity.get(vr.to_version_entity_id)
        if not from_eid or not to_eid:
            report["warnings"].append(f"关系 {vr.name} 的端点无法解析，已跳过")
            continue

        rid = vr.source_relation_id
        if rid:
            seen_rel_ids.add(rid)
            live_rel = db.get(EntityRelation, rid)
        else:
            live_rel = None

        if live_rel is None:
            new_rel = EntityRelation(
                id=rid or gen_uuid(),
                from_entity_id=from_eid,
                to_entity_id=to_eid,
                name=vr.name,
                rel_type=vr.rel_type or "association",
                cardinality=vr.cardinality or "1:n",
                acyclic=bool(vr.acyclic),
                description=vr.description,
            )
            db.add(new_rel)
            db.flush()
            vr.source_relation_id = new_rel.id
            seen_rel_ids.add(new_rel.id)
            report["relationsCreated"] += 1
        else:
            changed = False
            for f in _REL_FIELDS:
                if f == "acyclic":
                    new_val = bool(getattr(vr, f))
                else:
                    new_val = getattr(vr, f)
                if getattr(live_rel, f) != new_val:
                    setattr(live_rel, f, new_val)
                    changed = True
            if live_rel.from_entity_id != from_eid:
                live_rel.from_entity_id = from_eid
                changed = True
            if live_rel.to_entity_id != to_eid:
                live_rel.to_entity_id = to_eid
                changed = True
            if changed:
                report["relationsUpdated"] += 1

    live_rels = db.query(EntityRelation).all()
    for lr in live_rels:
        if lr.id not in seen_rel_ids:
            report["extras"]["relations"].append(
                {"id": lr.id, "name": lr.name}
            )

    report["appliedAt"] = datetime.utcnow().isoformat()
    return report


def preview_apply(db: Session, version: OntologyVersion) -> dict[str, Any]:
    """只算会改什么，不落库（给"审批前预览"用）。"""
    from app.services.version_diff import compare_versions

    res = compare_versions(db, version.id)
    changes = res["changes"]
    return {
        "version": res["version"],
        "baseline": res["baseline"],
        "changes": changes,
        "breakingCount": res["breakingCount"],
        "willUpdate": {
            "entities": sum(1 for c in changes
                            if c["scope"] == "entity" and c["action"] in ("renamed", "modified")),
            "entitiesCreated": sum(1 for c in changes
                                   if c["scope"] == "entity" and c["action"] == "added"),
            # 新增实体自带的属性也要算进去，否则"新建 3 个实体"会显示"属性 0"，
            # 让人以为新实体是空壳。
            "attributes": _count_attribute_changes(db, version, changes),
            "relations": sum(1 for c in changes if c["scope"] == "relation"),
        },
        "dependents": res["dependents"],
    }


def _count_attribute_changes(db: Session, version: OntologyVersion, changes: list[dict]) -> int:
    """属性变更总数 = 已有实体的属性改动 + 新增实体自带的属性。

    只数前者会让"新建 3 个实体"显示成"属性 0"，看起来像要建 3 个空壳。
    """
    from app.models.entity import OntologyEntity

    n = sum(1 for c in changes if c["scope"] == "attribute")
    live_ids = {x[0] for x in db.query(OntologyEntity.id).all()}
    new_entities = db.query(OntologyVersionEntity).filter(
        OntologyVersionEntity.version_id == version.id
    ).all()
    for ve in new_entities:
        if ve.source_entity_id in live_ids:
            continue
        n += db.query(OntologyVersionAttribute).filter(
            OntologyVersionAttribute.version_entity_id == ve.id
        ).count()
    return n
