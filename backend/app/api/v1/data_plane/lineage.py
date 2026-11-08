"""/lineage v2 — 本体侧血缘（Asset → ObjectType → Action）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.data_plane import LineageGraph
from app.services.data_plane.lineage_service import LineageService

router = APIRouter(prefix="/lineage", tags=["data-plane:lineage"])


@router.get("/overview", response_model=LineageGraph)
def get_overview(
    detail: str = Query("coarse", pattern="^(coarse|full)$"),
    db: Session = Depends(get_db),
):
    """全局血缘大图。

    `detail=coarse`（默认）是"资产 → 对象类型 → 动作/规则"；
    `detail=full` 追加字段级链路（指标 → 物理列 → 对象属性）。
    """
    return LineageService(db).get_overview(detail=detail)


@router.post("/rebuild")
def rebuild_lineage(
    include_fields: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """从当前配置重建血缘边。

    分两层：
      · 配置事实（对象绑了哪张表、挂了哪些动作/规则）—— 由 lineage_builder 重建
      · SQL 事实（指标表达式读了哪些列）—— 由 field_lineage 解析 cube 模型

    两层都是确定性的，跑十次结果一样。对象索引同步后会自动调一次。
    """
    from app.services.data_plane.lineage_builder import rebuild

    result: dict = {"config": rebuild(db)}
    if include_fields:
        from app.services.data_plane.field_lineage import build as build_fields

        result["fields"] = build_fields(db)
    return result


@router.get("", response_model=LineageGraph)
def get_lineage(
    asset_id: str | None = Query(None),
    object_type_id: str | None = Query(None),
    depth: int = Query(2, ge=1, le=5),
    db: Session = Depends(get_db),
):
    if asset_id and object_type_id:
        raise HTTPException(400, "asset_id 与 object_type_id 二选一")
    if asset_id:
        return LineageService(db).get_for_asset(asset_id, depth=depth)
    if object_type_id:
        return LineageService(db).get_for_object_type(object_type_id, depth=depth)
    raise HTTPException(400, "asset_id 或 object_type_id 必填")
