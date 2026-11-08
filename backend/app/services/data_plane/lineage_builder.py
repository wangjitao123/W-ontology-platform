"""数据血缘构建器 —— 把"这张表喂给了哪个对象类型"变成可查询的边。

## 为什么菜单是空的

`lineage_edges` 表一直是 0 行。查下来：`LineageEdgeRepository.upsert()` 只有
import 和读操作，**没有任何调用方**——原项目把"记录血缘"的写入端漏掉了，
只做了读端和前端图。

设计意图从 repo 能看出来：`upsert` 每调一次 weight 就 +1，`last_seen_at` 跟着
刷新，说明本意是**在每次执行时记一次信号**。但记录器没接上，所以一直是空。

## 这里怎么补

不依赖"运行时碰巧被记录"，而是**从当前配置确定性重建**：

    asset  --binds_to-->  object_type     来自 ObjectBinding（对象类型绑了哪张表）
    object_type --has_action--> action   来自 EntityAction
    object_type --has_rule-->  rule      来自 BusinessRule

weight 用 `execution_logs` 里该资产的实际执行次数，这样图上的粗细有真实含义
（"这条数据通路被真正读过 536 次"），不是编出来的数字。

**重建是幂等的**：边集合完全由配置决定；配置里已经不存在的边会被置 weight=0
（保留历史但不显示，`get_overview` 过滤 `weight > 0`）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.execution_log import ExecutionLog
from app.models.lineage_edge import LineageEdge

logger = logging.getLogger(__name__)

# 本构建器负责的关系；其他关系（如果有）不动
OWNED_RELATIONS = ("binds_to", "has_action", "has_rule")


def _upsert(
    db: Session,
    *,
    source_kind: str,
    source_id: str,
    target_kind: str,
    target_id: str,
    relation: str,
    weight: int,
    via_module: str | None,
    via_purpose: str | None,
) -> str:
    """写一条边，返回 "created" / "updated" / "unchanged"。

    与 repo.upsert 的区别：那个是"每调用一次 +1"的信号累加器，这里要确定性
    覆盖（重建跑十次结果必须一样），所以直接写算出来的 weight。
    """
    now = datetime.utcnow()
    edge = (
        db.query(LineageEdge)
        .filter(
            LineageEdge.source_kind == source_kind,
            LineageEdge.source_id == source_id,
            LineageEdge.target_kind == target_kind,
            LineageEdge.target_id == target_id,
            LineageEdge.relation == relation,
        )
        .first()
    )
    if edge is None:
        db.add(LineageEdge(
            source_kind=source_kind, source_id=source_id,
            target_kind=target_kind, target_id=target_id,
            relation=relation,
            via_module=via_module, via_purpose=via_purpose,
            weight=max(1, int(weight)),
            first_seen_at=now, last_seen_at=now,
        ))
        return "created"

    changed = False
    if edge.weight != max(1, int(weight)):
        edge.weight = max(1, int(weight))
        changed = True
    if via_module and edge.via_module != via_module:
        edge.via_module = via_module
        changed = True
    if via_purpose and edge.via_purpose != via_purpose:
        edge.via_purpose = via_purpose
        changed = True
    edge.last_seen_at = now
    return "updated" if changed else "unchanged"


def _asset_purpose_weights(db: Session) -> dict[str, tuple[str | None, int]]:
    """每个资产执行次数最多的 purpose 和次数。

    这是图上 weight 的真实来源：读得越多，通路越粗。
    """
    rows = (
        db.query(
            ExecutionLog.asset_id,
            ExecutionLog.purpose,
            func.count(ExecutionLog.id),
        )
        .filter(ExecutionLog.asset_id.isnot(None))
        .group_by(ExecutionLog.asset_id, ExecutionLog.purpose)
        .all()
    )
    best: dict[str, tuple[str | None, int]] = {}
    for asset_id, purpose, n in rows:
        prev = best.get(asset_id)
        if prev is None or n > prev[1]:
            best[asset_id] = (purpose, int(n))
    return best


def rebuild(db: Session) -> dict[str, Any]:
    """从当前配置重建血缘边，返回统计。"""
    from app.models.entity import OntologyEntity
    from app.models.object_binding import ObjectBinding
    from app.models.rule import BusinessRule, EntityAction

    stats = {"created": 0, "updated": 0, "unchanged": 0, "deprecated": 0}
    seen: set[tuple[str, str, str, str, str]] = set()

    purposes = _asset_purpose_weights(db)

    # ── 1) asset --binds_to--> object_type ──
    bindings = db.query(ObjectBinding).all()
    entity_ids = {e.id for e in db.query(OntologyEntity).all()}
    for b in bindings:
        if b.object_type_id not in entity_ids:
            continue
        purpose, n = purposes.get(b.asset_id, (None, 1))
        key = ("asset", b.asset_id, "object_type", b.object_type_id, "binds_to")
        seen.add(key)
        stats[_upsert(
            db,
            source_kind="asset", source_id=b.asset_id,
            target_kind="object_type", target_id=b.object_type_id,
            relation="binds_to",
            # 没被执行过的资产给权重 1（配置上存在就算一条通路）
            weight=max(1, n),
            via_module=f"object_binding:{b.role}",
            via_purpose=purpose,
        )] += 1

    # ── 2) object_type --has_action--> action ──
    for a in db.query(EntityAction).all():
        if not a.entity_id or a.entity_id not in entity_ids:
            continue
        key = ("object_type", a.entity_id, "action", a.id, "has_action")
        seen.add(key)
        stats[_upsert(
            db,
            source_kind="object_type", source_id=a.entity_id,
            target_kind="action", target_id=a.id,
            relation="has_action", weight=1,
            via_module="entity_action", via_purpose=None,
        )] += 1

    # ── 3) object_type --has_rule--> rule ──
    for r in db.query(BusinessRule).all():
        if not r.entity_id or r.entity_id not in entity_ids:
            continue
        key = ("object_type", r.entity_id, "rule", r.id, "has_rule")
        seen.add(key)
        stats[_upsert(
            db,
            source_kind="object_type", source_id=r.entity_id,
            target_kind="rule", target_id=r.id,
            relation="has_rule", weight=1,
            via_module="business_rule", via_purpose=None,
        )] += 1

    # ── 4) 配置里已经消失的边：置 weight=0（保留历史，不显示）──
    stale = (
        db.query(LineageEdge)
        .filter(LineageEdge.relation.in_(OWNED_RELATIONS))
        .all()
    )
    now = datetime.utcnow()
    for e in stale:
        k = (e.source_kind, e.source_id, e.target_kind, e.target_id, e.relation)
        if k not in seen and (e.weight or 0) > 0:
            e.weight = 0
            e.last_seen_at = now
            stats["deprecated"] += 1

    db.commit()
    total = (
        db.query(func.count(LineageEdge.id))
        .filter(LineageEdge.weight > 0)
        .scalar() or 0
    )
    logger.info("血缘: 重建完成 %s，当前有效边 %s", stats, total)
    return {**stats, "activeEdges": int(total)}
