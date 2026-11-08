"""语义层 API —— 约束 / 证据 / 总览。

这三个接口存在的意义：让"我们凭什么认为这个口径是对的"变得可查、可复核。

    GET /semantics/overview      语义层有多少内容（约束数、别名覆盖率、粒度覆盖率）
    GET /semantics/constraints   全部约束（可按 target / severity 过滤）
    GET /semantics/evidence      全部证据（含实际执行的查询和结果）

约束和证据是**只读**的：它们由 seed 脚本维护（见 scripts/seed_semantic_layer.py），
不通过 API 写。这样避免"运行时随手改约束"导致语义漂移——
和指标口径一样，语义变更应该走代码评审。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import semantics_service

router = APIRouter(prefix="/semantics", tags=["semantics"])


@router.get("/overview")
def semantics_overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    """语义层总览：约束、证据、术语（别名/粒度）覆盖率。"""
    return semantics_service.overview(db)


@router.get("/constraints")
def list_constraints(
    target_kind: str | None = Query(None, description="entity / attribute / relation / metric / cube"),
    target_id: str | None = Query(None),
    severity: str | None = Query(None, description="block / warn / info"),
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """业务约束列表，按严重级别 + 优先级排序。"""
    return semantics_service.list_constraints(
        db,
        target_kind=target_kind,
        target_id=target_id,
        severity=severity,
        include_inactive=include_inactive,
    )


@router.get("/evidence")
def list_evidence(
    source: str | None = Query(None, description="doris_query / cube / execution_log / data_profile"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """证据列表：每条语义结论背后的实际查询和结果。"""
    return semantics_service.list_evidence(db, source=source)
