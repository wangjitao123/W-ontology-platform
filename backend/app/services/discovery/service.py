"""自动发现的服务层：扫描出候选，采纳进版本。

## 采纳走的是版本流程，不是直接写库

采纳 = 把这些候选塞进一个**草稿版本**，然后走 ③ 那套 diff → 影响面 → 审批 → 生效。

这么做的好处是白拿的：

    · 采纳前能看到"这次会新增什么"（diff）
    · 能撤销（草稿可以直接删）
    · 生效有回执（实际建了几个实体、几个属性）
    · 出事能回滚

如果采纳直接往 `ontology_entities` 插行，上面这些全都没有。

## 已存在的实体一律跳过

现有本体里已经有 Material / Customer / Supplier / Plant 这些。
候选里同样会出现它们（同一批列名反推出来的）。
重复采纳会产生同名不同源的"两个物料"，比不采纳更糟。

所以：`source_entity_id` 已存在于 `ontology_entities` 的候选直接跳过，
并在结果里如实列出 `skipped`——让人知道"不是没发现，是已经有了"。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.models.entity import OntologyEntity
from app.models.version import (
    OntologyVersion,
    OntologyVersionAttribute,
    OntologyVersionEntity,
    OntologyVersionRelation,
)
from app.services.discovery.profiler import scan
from app.services.discovery.rules import build_proposals
from app.utils.identifiers import gen_uuid

logger = logging.getLogger(__name__)

# 扫描结果缓存：165 张表 4246 列的元数据查询 ~1-2 秒，
# 但采纳时要按 id 回查候选，不能每次都重扫。
_CACHE_TTL_SECONDS = 120.0
_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _annotate_existing(db, data: dict[str, Any]) -> None:
    """给候选标注"和现有本体是什么关系"。

    自动发现只能看到表结构，它不知道你已经手工建了 `ContractVehicle`。
    于是它会把 `Contract` 也推出来——**不是重复 id，但概念重叠**。
    如果只在采纳时按 id 判重，用户会得到两个"合同"。

    所以这里在扫描阶段就标注：
        existing === true          同名已存在（采纳时会被跳过）
        similarTo: ["ContractVehicle"]  名字相近，可能是同一个东西
        missingAttributes               现有类型上缺的属性（可用来补全，而不是新建）
    """
    existing = {e.id: e for e in db.query(OntologyEntity).all()}
    existing_lower = {k.lower(): k for k in existing}

    for p in data.get("entities") or []:
        name = p["name"]
        low = name.lower()
        if low in existing_lower:
            real_id = existing_lower[low]
            p["existing"] = True
            p["existingId"] = real_id
            ent = existing[real_id]
            have = {(a.name or "").lower() for a in ent.attributes}
            missing = [a["name"] for a in p.get("attributes") or []
                       if a["name"].lower() not in have]
            p["missingAttributes"] = missing
            continue

        # 名字相近：互相是子串，或去掉常见后缀后相同
        similar = []
        for eid in existing:
            el = eid.lower()
            if low in el or el in low:
                similar.append(eid)
        if similar:
            p["similarTo"] = sorted(similar)


def get_proposals(db, *, include_snapshots: bool = True, force: bool = False) -> dict[str, Any]:
    """扫描并推导候选（带缓存）。"""
    now = time.monotonic()
    if (
        not force
        and _cache["data"] is not None
        and now - _cache["at"] < _CACHE_TTL_SECONDS
    ):
        return _cache["data"]

    tables = scan(db)
    data = build_proposals(tables, include_snapshots=include_snapshots)
    _annotate_existing(db, data)
    data["scannedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _cache["at"], _cache["data"] = now, data
    return data


def reset_cache() -> None:
    _cache["at"], _cache["data"] = 0.0, None


def _index_by_id(data: dict[str, Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for bucket in ("entities", "snapshots", "relations"):
        for p in data.get(bucket) or []:
            out[p["id"]] = p
    return out


def _copy_version_into(db, src_version_id: str, dst_version: OntologyVersion) -> dict[str, str]:
    """把某个版本的实体/属性/关系整份复制进新版本，返回 {源版本实体id: 新版本实体id}。

    ## 为什么采纳时必须先复制现有版本

    草稿版本的语义是**完整快照**，不是增量。如果采纳时只把 3 个新候选放进去，
    拿它和生效版本做 diff 会得到"删除实体 客户 / 物料 / ..."共 14 条——
    看起来像要干掉整个现有本体，实际只是草稿不完整。

    所以采纳 = 复制当前生效版本 + 追加候选。
    """
    src_entities = db.query(OntologyVersionEntity).filter(
        OntologyVersionEntity.version_id == src_version_id
    ).all()
    id_map: dict[str, str] = {}

    for se in src_entities:
        ne = OntologyVersionEntity(
            version_id=dst_version.id,
            source_entity_id=se.source_entity_id,
            name=se.name,
            name_cn=se.name_cn,
            tier=se.tier,
            description=se.description,
            config_json=se.config_json,
            publish_config=se.publish_config,
        )
        db.add(ne)
        db.flush()
        id_map[se.id] = ne.id

        for sa in db.query(OntologyVersionAttribute).filter(
            OntologyVersionAttribute.version_entity_id == se.id
        ).all():
            db.add(OntologyVersionAttribute(
                version_entity_id=ne.id,
                source_attribute_id=sa.source_attribute_id,
                name=sa.name,
                type=sa.type,
                description=sa.description,
                required=sa.required,
                example=sa.example,
                constraints_json=sa.constraints_json,
                source_table=sa.source_table,
                source_field=sa.source_field,
                data_status=sa.data_status,
            ))

    for sr in db.query(OntologyVersionRelation).filter(
        OntologyVersionRelation.version_id == src_version_id
    ).all():
        frm, to = id_map.get(sr.from_version_entity_id), id_map.get(sr.to_version_entity_id)
        if not frm or not to:
            continue
        db.add(OntologyVersionRelation(
            version_id=dst_version.id,
            source_relation_id=sr.source_relation_id,
            from_version_entity_id=frm,
            to_version_entity_id=to,
            name=sr.name,
            rel_type=sr.rel_type,
            cardinality=sr.cardinality,
            acyclic=sr.acyclic,
            description=sr.description,
        ))

    db.flush()
    return id_map


def adopt(

    db,
    proposal_ids: list[str],
    *,
    version_name: str | None = None,
    include_snapshots: bool = True,
) -> dict[str, Any]:
    """把选中的候选写成一份草稿版本。"""
    data = get_proposals(db, include_snapshots=include_snapshots)
    by_id = _index_by_id(data)

    unknown = [i for i in proposal_ids if i not in by_id]
    picked = [by_id[i] for i in proposal_ids if i in by_id]
    nodes = [p for p in picked if p["kind"] in ("entity", "snapshot", "report")]
    rels = [p for p in picked if p["kind"] == "relation"]

    if not nodes:
        return {
            "ok": False,
            "message": "没有选中任何对象类型候选（关系不能单独采纳）",
            "unknown": unknown,
        }

    existing = {e.id for e in db.query(OntologyEntity).all()}
    to_create = [p for p in nodes if p["name"] not in existing]
    skipped = [
        {"name": p["name"], "reason": "已存在于本体现有类型中"}
        for p in nodes if p["name"] in existing
    ]
    if not to_create:
        return {
            "ok": False,
            "message": "选中的对象类型都已存在，无需采纳",
            "skipped": skipped,
            "unknown": unknown,
        }

    max_num = db.query(OntologyVersion).count()
    v = OntologyVersion(
        version_number=int(time.time()) % 100000 + max_num,
        name=version_name or f"自动发现：采纳 {len(to_create)} 个对象类型",
        description=(
            f"由表结构自动推导并采纳。候选来源：{len(data.get('entities') or [])} 个实体候选、"
            f"{len(data.get('snapshots') or [])} 个快照候选。"
            "所有属性均来自真实列名，未人工确认前 data_status 标记为「待确认」。"
        ),
        status="draft",
    )
    # version_number 有唯一约束，用 max+1 更稳
    from sqlalchemy import func
    v.version_number = (db.query(func.max(OntologyVersion.version_number)).scalar() or 0) + 1
    db.add(v)
    db.flush()

    # 关键：先把当前生效版本整份复制进来。
    # 草稿是"完整快照"不是"增量"，只放新候选会让 diff 显示删掉现有 14 个实体。
    copied = 0
    active = db.query(OntologyVersion).filter(
        OntologyVersion.is_active == True,  # noqa: E712
    ).first()
    if active is not None:
        copied = len(_copy_version_into(db, active.id, v))

    created_entities: list[dict] = []
    name_to_ve: dict[str, str] = {}

    for p in to_create:
        ve = OntologyVersionEntity(
            version_id=v.id,
            # 用候选名做实际实体 id：apply 时会按这个 id 创建 OntologyEntity
            source_entity_id=p["name"],
            name=p["name"],
            name_cn=p["nameCn"] or p["name"],
            tier=2,
            description=p["reason"],
            config_json={
                "object_kind": "entity" if p["kind"] == "entity" else p["kind"],
                "discovered": True,
                "discovery_source": p.get("sources") or [],
                "identity_risk": bool(p.get("identityRisk")),
            },
        )
        db.add(ve)
        db.flush()
        name_to_ve[p["name"]] = ve.id

        for a in p.get("attributes") or []:
            db.add(OntologyVersionAttribute(
                version_entity_id=ve.id,
                # 库里 source_attribute_id 是 NOT NULL，不能留空表示"新建"。
                # 这里预生成一个 uuid：apply_snapshot_to_live 会按这个 id 去
                # EntityAttribute 里查，查不到就用**同一个 id** 创建。
                # 于是"待创建"这个语义靠"查不到"表达，不需要改表结构。
                source_attribute_id=gen_uuid(),
                name=a["name"],
                type=a.get("type") or "string",
                description="",
                required=False,
                source_table=a.get("sourceTable"),
                source_field=a.get("sourceField"),
                data_status="待确认",
            ))
        created_entities.append({
            "name": p["name"],
            "nameCn": p["nameCn"],
            "attributeCount": len(p.get("attributes") or []),
            "identityRisk": bool(p.get("identityRisk")),
        })

    created_rels: list[dict] = []
    for r in rels:
        frm = name_to_ve.get(r.get("from"))
        to = name_to_ve.get(r.get("to"))
        if not frm or not to:
            skipped.append({
                "name": r["name"],
                "reason": "关系两端有未采纳的对象类型",
            })
            continue
        db.add(OntologyVersionRelation(
            version_id=v.id,
            # 同上：预生成 id，apply 时按此 id 在新表里创建关系
            source_relation_id=gen_uuid(),
            from_version_entity_id=frm,
            to_version_entity_id=to,
            name=r["name"],
            rel_type=r.get("relType") or "association",
            cardinality="n:n",
            acyclic=False,
            description=r.get("reason"),
        ))
        created_rels.append({"name": r["name"], "cooccurrence": r.get("cooccurrence")})

    db.commit()
    db.refresh(v)

    return {
        "ok": True,
        "versionId": v.id,
        "copiedFromActive": copied,
        "versionNumber": v.version_number,
        "created": created_entities,
        "relations": created_rels,
        "skipped": skipped,
        "unknown": unknown,
        "next": f"调用 GET /ontology-publish/versions/{v.id}/diff 查看差异，"
                f"审批通过后才会真正创建对象类型",
    }
