"""对象集合（Object Set）查询 API —— 把「对象」当作可查询的数据源。

对应 Foundry 的 Object Set 能力里最常用的两块：
    - query：按属性等值过滤 + 标题搜索 + 分页
    - aggregate：按属性分组做 count / sum / min / max

数据来自对象索引表（object_index），不直接打源库 —— 所以要先
`POST /studio/index/sync` 建好索引，否则返回空集合 + 明确提示。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.entity import OntologyEntity
from app.services.data_plane.object_index_service import ObjectIndexService

router = APIRouter(prefix="/object-sets", tags=["object-sets"])


class ObjectQueryRequest(BaseModel):
    # 属性等值过滤，如 {"star_level": "五星"}
    filter: dict[str, Any] = Field(default_factory=dict)
    # 标题/主键模糊搜索
    search: str | None = None
    page: int = 1
    page_size: int = 50
    include_properties: bool = True
    # 把该属性有值的实例排前面（用于"按属性过滤指标"，避免点进去查不出指标）
    prefer_key: str | None = None


class MetricSpec(BaseModel):
    fn: Literal["count", "sum", "min", "max", "avg"]
    field: str | None = None   # count 时可省略


class ObjectAggregateRequest(BaseModel):
    group_by: list[str] = Field(default_factory=list)
    metrics: list[MetricSpec] = Field(default_factory=lambda: [MetricSpec(fn="count")])
    filter: dict[str, Any] = Field(default_factory=dict)


def _must_entity(db: Session, object_type: str) -> OntologyEntity:
    entity = db.get(OntologyEntity, object_type)
    if entity is None:
        # 兼容传中文名 / 大小写差异
        entity = (
            db.query(OntologyEntity)
            .filter(OntologyEntity.name == object_type)
            .first()
        )
    if entity is None:
        raise HTTPException(404, f"对象类型不存在: {object_type}")
    return entity


@router.post("/{object_type}/query")
def query_object_set(
    object_type: str,
    body: ObjectQueryRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按属性筛选 / 搜索 / 分页取对象实例。"""
    entity = _must_entity(db, object_type)
    svc = ObjectIndexService(db)
    if not svc.has_data():
        raise HTTPException(
            409,
            "对象索引为空，请先调用 POST /api/v1/studio/index/sync 建立索引",
        )

    result = svc.query(
        entity.id,
        filter=body.filter,
        search=body.search,
        page=body.page,
        page_size=body.page_size,
        include_props=body.include_properties,
        prefer_key=body.prefer_key,
    )
    return {
        "objectType": entity.name,
        "objectTypeDisplay": entity.name_cn,
        **result,
    }


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/{object_type}/{object_key}/links")
def object_links(
    object_type: str,
    object_key: str,
    limit: int = 200,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """这个对象关联了谁（双向，按关系分组）。

    与 `/studio/instance/{type}/{id}` 的区别：那条接口依赖实例层缓存（有节点数
    和边数上限），这里直接查物化的 object_link，结果完整且和缓存无关。
    """
    from app.services.object_link_service import neighbors

    entity = _must_entity(db, object_type)
    return neighbors(db, entity.id, object_key, limit=limit)


@router.get("/{object_type}/{object_key}/metrics")
def object_metrics(
    object_type: str,
    object_key: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """取「这个对象的指标」。

    按 `object_metric_bindings` 里配置的规则，用对象属性值去过滤 Cube 的维度，
    例如：财务月报对象 period=2026-07 → 查询 finance_summary 并限定 period。

    口径仍由 Cube 定义，这里只负责"怎么过滤"。单个绑定失败不影响其他绑定，
    每条结果都带自己的 error 字段。
    """
    from app.models.object_metric_binding import ObjectMetricBinding
    from app.services.data_plane.object_index_service import ObjectIndexService
    from app.services.metrics_cube import CubeClient, CubeError

    entity = _must_entity(db, object_type)

    bindings = (
        db.query(ObjectMetricBinding)
        .filter(
            ObjectMetricBinding.object_type_id == entity.id,
            ObjectMetricBinding.status == "active",
        )
        .all()
    )
    if not bindings:
        return {
            "objectType": entity.name,
            "objectKey": object_key,
            "metrics": [],
            "note": "该对象类型未配置任何指标绑定",
        }

    props = ObjectIndexService(db).get_props(entity.id, object_key)
    if props is None:
        raise HTTPException(404, f"对象实例不存在: {entity.name}:{object_key}")

    client = CubeClient()
    results: list[dict[str, Any]] = []

    for b in bindings:
        raw_value = props.get(b.key_column)
        base = {
            "label": b.label or f"{b.cube_name} / {b.key_column}",
            "cube": b.cube_name,
            "keyColumn": b.key_column,
            "filterMember": b.resolved_filter_member(),
            "filterValue": None if raw_value is None else str(raw_value),
            "data": [],
            "error": None,
        }

        if raw_value in (None, ""):
            base["error"] = {
                "reason": "missing_key",
                "message": f"对象缺少属性 {b.key_column}，无法过滤指标",
            }
            results.append(base)
            continue

        measures = [
            m if "." in m else f"{b.cube_name}.{m}"
            for m in (b.metric_names or [])
        ]
        if not measures:
            base["error"] = {"reason": "no_metrics", "message": "绑定未配置 metric_names"}
            results.append(base)
            continue

        filters = [
            {
                "member": b.resolved_filter_member(),
                "operator": "equals",
                "values": [str(raw_value)],
            },
            *(b.extra_filters or []),
        ]

        try:
            resp = client.load({"measures": measures, "filters": filters, "limit": 10})
            base["data"] = resp.get("data") or []
        except CubeError as e:
            base["error"] = {"reason": e.reason, "message": e.message}

        results.append(base)

    return {
        "objectType": entity.name,
        "objectTypeDisplay": entity.name_cn,
        "objectKey": object_key,
        "metrics": results,
    }


@router.post("/{object_type}/aggregate")
def aggregate_object_set(
    object_type: str,
    body: ObjectAggregateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按属性分组聚合。

    属性在索引里是字符串化的 JSON，因此在 Python 侧做聚合；
    索引规模受 MAX_ROWS_PER_TYPE 约束，单类型最多 5000 行。
    """
    entity = _must_entity(db, object_type)
    svc = ObjectIndexService(db)
    if not svc.has_data():
        raise HTTPException(
            409,
            "对象索引为空，请先调用 POST /api/v1/studio/index/sync 建立索引",
        )

    # 先取出全部行（分页给一个大值，聚合需要全量）
    full = svc.query(
        entity.id,
        filter=body.filter,
        page=1,
        page_size=5000,
        include_props=True,
    )
    rows = [item.get("properties") or {} for item in full.get("items", [])]

    buckets: dict[tuple, list[dict[str, Any]]] = {}
    for props in rows:
        key = tuple(str(props.get(f)) if props.get(f) is not None else None
                    for f in body.group_by)
        buckets.setdefault(key, []).append(props)

    out: list[dict[str, Any]] = []
    for key, group in buckets.items():
        row: dict[str, Any] = {
            (f or f"col{i}"): key[i] for i, f in enumerate(body.group_by)
        }
        for m in body.metrics:
            if m.fn == "count":
                row[f"count"] = len(group)
                continue
            if not m.field:
                raise HTTPException(400, f"指标 {m.fn} 需要指定 field")
            values = [
                v for v in (_to_number(p.get(m.field)) for p in group)
                if v is not None
            ]
            alias = f"{m.fn}_{m.field}"
            if not values:
                row[alias] = None
            elif m.fn == "sum":
                row[alias] = round(sum(values), 4)
            elif m.fn == "min":
                row[alias] = min(values)
            elif m.fn == "max":
                row[alias] = max(values)
            else:  # avg
                row[alias] = round(sum(values) / len(values), 4)
        out.append(row)

    # 计数降序，便于直接展示"最多的分组"
    out.sort(key=lambda r: r.get("count", 0), reverse=True)

    return {
        "objectType": entity.name,
        "objectTypeDisplay": entity.name_cn,
        "sampledRows": len(rows),
        "groups": out,
    }
