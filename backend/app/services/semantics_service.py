"""语义层服务：约束查询 + 按问题匹配相关约束。

## 为什么要"按问题匹配"

约束写在库里不等于有用。真正的价值在于：用户问"库龄超过 180 天的库存有多少"时，
系统主动告诉他「这条口径有坑：库龄表只有单一快照日，跨天直接 SUM 会重复计算」。

匹配依据两条：
    1. `trigger_keywords` 命中问题文本（用户口语 → 约束）
    2. `target_id` 命中本次查询涉及的对象类型 / cube（结构定位）

两者是 OR 关系：命中关键词但没定位到对象，仍然提示（宁可多提示）；
定位到对象但没命中关键词，也提示（因为问题一定涉及那个对象的口径）。

## 严重级别

    block —— 违反会得出错误数字，回答时必须显式提示
    warn  —— 容易错，提示但不阻断
    info  —— 背景说明

当前**只做提示，不做硬拦截**：要硬拦截需要能解析查询结构判断是否违反，
那是另一个量级的工作（需要在 cube 查询生成后做 AST 级校验）。
所以这里的诚实定位是"护栏提示"，不是"自动纠错"。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.semantic import SemanticConstraint, SemanticEvidence

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"block": 0, "warn": 1, "info": 2}


def list_constraints(
    db: Session,
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
    severity: str | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    q = db.query(SemanticConstraint)
    if not include_inactive:
        q = q.filter(SemanticConstraint.status == "active")
    if target_kind:
        q = q.filter(SemanticConstraint.target_kind == target_kind)
    if target_id:
        q = q.filter(SemanticConstraint.target_id == target_id)
    if severity:
        q = q.filter(SemanticConstraint.severity == severity)

    rows = q.all()
    rows.sort(key=lambda c: (_SEVERITY_RANK.get(c.severity, 9), c.priority or 100))
    # 约束总量很小（个位数到几十条），带上证据一起返回，前端一次就能展开看依据
    return [_constraint_dict(db, c, with_evidence=True) for c in rows]


def _constraint_dict(
    db: Session, c: SemanticConstraint, *, with_evidence: bool = True,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": c.id,
        "name": c.name,
        "targetKind": c.target_kind,
        "targetId": c.target_id,
        "constraintType": c.constraint_type,
        "severity": c.severity,
        "description": c.description,
        "remedy": c.remedy,
        "triggerKeywords": c.trigger_keywords or [],
        "scope": c.scope,
        "autoSurface": bool(c.auto_surface),
        # 自动检测：有 detector 说明这条约束能在执行前检查；
        # enforce=True 且 severity=block 时，违反会被真正拦截（不只是提示）。
        "detector": (c.detector_json or {}).get("type"),
        "enforce": bool(c.enforce),
    }
    if with_evidence:
        ids = c.evidence_ids or []
        if ids:
            evs = (
                db.query(SemanticEvidence)
                .filter(SemanticEvidence.id.in_(ids))
                .all()
            )
            by_id = {e.id: e for e in evs}
            out["evidence"] = [
                {
                    "id": eid,
                    "title": by_id[eid].title,
                    "source": by_id[eid].source,
                    "query": by_id[eid].query,
                    "result": by_id[eid].result,
                    "validationMethod": by_id[eid].validation_method,
                }
                for eid in ids
                if eid in by_id
            ]
    return out


def list_evidence(db: Session, *, source: str | None = None) -> list[dict[str, Any]]:
    q = db.query(SemanticEvidence)
    if source:
        q = q.filter(SemanticEvidence.source == source)
    return [
        {
            "id": e.id,
            "title": e.title,
            "source": e.source,
            "query": e.query,
            "result": e.result,
            "validationMethod": e.validation_method,
            "observedAt": e.observed_at.isoformat() if e.observed_at else None,
        }
        for e in q.all()
    ]


def overview(db: Session) -> dict[str, Any]:
    """语义层总览：给前端一屏看清"我们有多少治理内容"。"""
    constraints = db.query(SemanticConstraint).all()
    by_sev: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for c in constraints:
        by_sev[c.severity] = by_sev.get(c.severity, 0) + 1
        by_type[c.constraint_type] = by_type.get(c.constraint_type, 0) + 1

    from app.models.entity import EntityAttribute, OntologyEntity
    from app.models.relation import EntityRelation

    entities = db.query(OntologyEntity).all()
    attrs = db.query(EntityAttribute).all()
    rels = db.query(EntityRelation).all()

    return {
        "constraints": {
            "total": len(constraints),
            "bySeverity": by_sev,
            "byType": by_type,
        },
        "evidence": {"total": db.query(SemanticEvidence).count()},
        "terms": {
            "entities": len(entities),
            "withAliases": sum(1 for e in entities if e.aliases),
            "attributes": len(attrs),
            "withGrain": sum(1 for a in attrs if a.grain),
            "withAggregation": sum(1 for a in attrs if a.aggregation_semantics),
            "withSourceField": sum(1 for a in attrs if a.source_field),
        },
        "relations": {
            "total": len(rels),
            "bySemanticType": _count_by(rels, "semantic_type"),
        },
    }


def _count_by(rows, field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        v = getattr(r, field, None) or "unknown"
        out[v] = out.get(v, 0) + 1
    return out


def relevant_constraints(
    db: Session,
    question: str,
    *,
    targets: list[tuple[str, str]] | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """找出与这个问题相关的约束。

    `targets` 是本次查询涉及的结构位置，如 [("cube", "finance_summary"),
    ("entity", "ContractVehicle")]。命中关键词或命中 target 都算相关。
    """
    q = (question or "").lower()
    target_set = {(k, i) for k, i in (targets or [])}

    hits: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for c in db.query(SemanticConstraint).filter(
        SemanticConstraint.status == "active",
        SemanticConstraint.auto_surface.is_(True),
    ).all():
        matched = [
            k for k in (c.trigger_keywords or [])
            if str(k).strip() and str(k).lower() in q
        ]
        target_hit = (c.target_kind, c.target_id) in target_set
        if not matched and not target_hit:
            continue
        # 排序：严重级别优先，其次关键词命中数（命中越多越相关）
        rank = (
            _SEVERITY_RANK.get(c.severity, 9),
            -len(matched) if matched else 0,
        )
        hits.append((rank, {
            "id": c.id,
            "name": c.name,
            "severity": c.severity,
            "constraintType": c.constraint_type,
            "targetKind": c.target_kind,
            "targetId": c.target_id,
            "description": c.description,
            "remedy": c.remedy,
            "matchedKeywords": matched,
            "matchedBy": "target" if target_hit and not matched else "keyword",
        }))

    hits.sort(key=lambda x: x[0])
    return [h for _, h in hits[:limit]]
