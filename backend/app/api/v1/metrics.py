"""指标层 API —— 把 Cube 的指标能力暴露给元枢前端/Agent。

分工：
    元枢 管"对象是谁、和谁有关系、能对它做什么"
    Cube 管"指标口径、维度、聚合、预聚合"

这里只做**转发与目录**，不重新定义任何口径 —— 口径的单一真相始终在 Cube 的
模型 YAML 里。元枢若也定义一份，两边必然漂移。

目录接口带 60 秒缓存：/meta 有 300KB+，不该每次点击都拉一遍。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.metrics_cube import CubeClient, CubeError

router = APIRouter(prefix="/metrics", tags=["metrics"])

_CATALOG_TTL_SECONDS = 60.0
_catalog_cache: tuple[float, list[dict[str, Any]]] | None = None


class MetricQueryRequest(BaseModel):
    # Cube 原生 query：{"measures": [...], "dimensions": [...], "filters": [...],
    #                   "timeDimensions": [...], "limit": n}
    query: dict[str, Any] = Field(default_factory=dict)


def _wrap(exc: CubeError) -> HTTPException:
    """把 Cube 错误转成结构化 HTTP 错误，保留原因码供前端区分。"""
    status = {
        "not_configured": 503,
        "unreachable": 503,
        "unauthorized": 502,
        "server_error": 502,
        "bad_response": 502,
        "query_failed": 502,
        "query_timeout": 504,
    }.get(exc.reason, 502)
    return HTTPException(status, {"reason": exc.reason, "message": exc.message})


def _catalog() -> list[dict[str, Any]]:
    global _catalog_cache
    now = time.monotonic()
    if _catalog_cache is not None and now - _catalog_cache[0] < _CATALOG_TTL_SECONDS:
        return _catalog_cache[1]
    cubes = CubeClient().list_cubes()
    _catalog_cache = (now, cubes)
    return cubes


@router.get("/health")
def metrics_health() -> dict[str, Any]:
    """探测 Cube 是否可达、模型是否编译通过。"""
    try:
        cubes = CubeClient().list_cubes()
    except CubeError as e:
        return {"ok": False, "reason": e.reason, "message": e.message}
    return {
        "ok": True,
        "cubeCount": len(cubes),
        "measureCount": sum(len(c["measures"]) for c in cubes),
        "dimensionCount": sum(len(c["dimensions"]) for c in cubes),
    }


@router.get("/catalog")
def metrics_catalog() -> dict[str, Any]:
    """全部 cube 及其指标/维度（不含自动生成的数据集视图）。"""
    try:
        cubes = _catalog()
    except CubeError as e:
        raise _wrap(e) from None
    return {
        "cubeCount": len(cubes),
        "measureCount": sum(len(c["measures"]) for c in cubes),
        "dimensionCount": sum(len(c["dimensions"]) for c in cubes),
        "cubes": cubes,
    }


@router.get("/catalog/{cube_name}")
def metrics_cube_detail(cube_name: str) -> dict[str, Any]:
    """单个 cube 的指标与维度。"""
    try:
        cubes = _catalog()
    except CubeError as e:
        raise _wrap(e) from None
    for c in cubes:
        if c["name"] == cube_name:
            return c
    raise HTTPException(404, f"cube 不存在: {cube_name}")


@router.post("/query")
def metrics_query(body: MetricQueryRequest) -> dict[str, Any]:
    """转发一次 Cube 查询。

    body.query 用 Cube 原生语法，例如：
        {"measures": ["finance_summary.total_assets"],
         "dimensions": ["finance_summary.month"],
         "limit": 12}

    数据源不可达时会返回 502 + reason=query_failed，并带上 Cube 原始错误，
    不会返回空结果假装成功。
    """
    if not body.query.get("measures"):
        raise HTTPException(400, {"reason": "missing_measures", "message": "query.measures 不能为空"})
    try:
        result = CubeClient().load(body.query)
    except CubeError as e:
        raise _wrap(e) from None
    return {
        "data": result.get("data") or [],
        "annotation": result.get("annotation") or {},
        "rowCount": len(result.get("data") or []),
    }


@router.get("/object-bindings")
def object_metric_bindings(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """哪些对象类型配了指标绑定。

    前端用它做两件事：给对象类型列表打标（有/无指标），
    以及默认选中"确实有指标"的那个类型，避免一进来就是空的。
    """
    from app.models.entity import OntologyEntity
    from app.models.object_metric_binding import ObjectMetricBinding

    rows = (
        db.query(ObjectMetricBinding)
        .filter(ObjectMetricBinding.status == "active")
        .all()
    )
    out: list[dict[str, Any]] = []
    for b in rows:
        entity = db.get(OntologyEntity, b.object_type_id)
        kind = ((entity.config_json or {}) if entity else {}).get("object_kind") or "entity"
        out.append({
            "objectTypeId": b.object_type_id,
            "objectTypeDisplay": entity.name_cn if entity else b.object_type_id,
            # 指标绑定只该挂在 entity/snapshot 上。report 是聚合结果，
            # "这个对象的指标"对它没有意义，所以显式给前端一个开关。
            "objectKind": kind,
            "bindingsAllowed": kind in ("entity", "snapshot"),
            "label": b.label,
            "cube": b.cube_name,
            "keyColumn": b.key_column,
            "filterMember": b.resolved_filter_member(),
            "metrics": b.metric_names or [],
        })
    return out


@router.post("/object-filters/sync")
def sync_object_filters(
    dry_run: bool = True,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """把 Cube 模型里的 WHERE 同步到对象类型的 ``filter_expr``。

    为什么需要：cube 的 SQL 带过滤（如 ``WHERE sign_state = 4``），对象集合也必须
    收窄到同一口径，否则"对象存在但指标查出 0"。手写一次会漂，所以做成可重复执行。

    默认 ``dry_run=true``，只报告会改什么。确认后传 ``dry_run=false`` 落库。
    标了 ``filter_expr_manual`` 的对象不会被覆盖。
    """
    from pathlib import Path as _Path

    from app.config import settings as _settings
    from app.services.cube_filter_sync import sync as _sync

    model_dir = _Path(_settings.CUBE_MODEL_DIR or "")
    if not model_dir.is_dir():
        raise HTTPException(
            400,
            {
                "reason": "model_dir_missing",
                "message": f"Cube 模型目录不存在: {model_dir}",
            },
        )
    report = _sync(db, model_dir, dry_run=dry_run)
    return report.to_dict()


@router.get("/refresh")
def metrics_refresh() -> dict[str, Any]:
    """清空目录缓存（Cube 模型更新后调用）。"""
    global _catalog_cache
    _catalog_cache = None
    return {"ok": True}
