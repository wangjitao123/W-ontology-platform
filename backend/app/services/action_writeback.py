"""动作写回桥接层 —— 把动作的写意图翻译成受治理的 SQL。

为什么要单独一层：
    动作执行器的入参是"本体语义"（实体 + 属性 + 实例主键），而写库需要
    "物理语义"（表名 + 列名 + 方言引号）。翻译逻辑集中在这里，执行器保持薄。
    真正的安全闸在 ExecuteService：
        - AST 审查（拒绝 DDL、非白名单表）
        - ALLOWED_WRITE_PURPOSES（默认空集合，需 ACTION_WRITEBACK_ENABLED）
        - Connection.writable
    本层只负责把语义映射到正确的表/列，不绕过任何一道闸。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import OntologyEntity
from app.models.asset import Asset
from app.models.object_binding import ObjectBinding

logger = logging.getLogger(__name__)


def _primary_binding(db: Session, entity_id: str) -> ObjectBinding | None:
    from app.repositories.object_binding_repo import ObjectBindingRepository
    return ObjectBindingRepository(db).get_primary(entity_id)


def _quote_for(db: Session, connection_id: str | None) -> str:
    from app.connectors import ConnectorRegistry
    from app.models.connection import Connection

    conn = db.get(Connection, connection_id) if connection_id else None
    return ConnectorRegistry.get(conn.type).quote_identifier() if conn else '"'


def _resolve_attribute(entity: OntologyEntity, attribute: str) -> str | None:
    """属性名 → 物理列名。优先用属性映射，没有就按同名处理。"""
    for a in entity.attributes:
        if a.name == attribute:
            return a.source_field or a.name
    return attribute or None


def _resolve_pk(db: Session, entity: OntologyEntity, binding: ObjectBinding) -> str | None:
    if binding.id_column:
        return binding.id_column
    for a in entity.attributes:
        if a.required and a.name.endswith("_id"):
            return a.source_field or a.name
    return None


def update_object_attribute(
    db: Session,
    entity_id: str,
    object_key: str,
    attribute: str,
    value: Any,
) -> dict[str, Any]:
    """把某个实例的某个属性改为 value。

    返回 {"ok": bool, "message": str, "output": {...}}；不抛异常，由调用方决定
    如何呈现（执行器需要把失败变成 ExecutionResult 而不是 500）。
    """
    from app.services.data_plane.execute_service import (
        ExecuteBlocked, ExecuteRequest, ExecuteService,
    )

    entity = db.get(OntologyEntity, entity_id)
    if entity is None:
        return {"ok": False, "message": f"实体不存在: {entity_id}"}

    binding = _primary_binding(db, entity_id)
    if binding is None:
        return {"ok": False, "message": f"实体 {entity.name} 没有任何数据绑定，无法写回"}

    asset = db.get(Asset, binding.asset_id)
    if asset is None:
        return {"ok": False, "message": f"绑定的资产不存在: {binding.asset_id}"}

    table = (asset.locator or {}).get("table") or ""
    if not table:
        return {"ok": False, "message": f"资产 {asset.name} 未配置表名"}

    column = _resolve_attribute(entity, attribute)
    pk_column = _resolve_pk(db, entity, binding)
    if not column or not pk_column:
        return {"ok": False, "message": "无法解析属性列或主键列"}

    quote = _quote_for(db, asset.connection_id)
    sql = (
        f"UPDATE {quote}{table}{quote} "
        f"SET {quote}{column}{quote} = :new_value "
        f"WHERE {quote}{pk_column}{quote} = :object_key"
    )

    try:
        result = ExecuteService(db).execute(ExecuteRequest(
            asset_id=asset.id,
            sql=sql,
            params={"new_value": value, "object_key": object_key},
            purpose="action.modify_attribute",
        ))
    except ExecuteBlocked as e:
        return {"ok": False, "message": f"写回被拒绝：{e.reason} {e.detail or ''}".strip()}
    except Exception as e:  # noqa: BLE001 - 执行失败要转成可展示的结果
        logger.warning("action writeback failed: %s", e)
        return {"ok": False, "message": f"写回失败：{e}"}

    return {
        "ok": True,
        "message": f"已更新 {entity.name}.{attribute} = {value}（实例 {object_key}）",
        "output": {
            "entity_id": entity_id,
            "attribute": attribute,
            "column": column,
            "table": table,
            "object_key": object_key,
            "rows_affected": getattr(result, "rows_returned", None),
        },
    }


def execute_asset_sql(
    db: Session, asset_id: str, sql: str, params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在指定资产上执行一段 SQL（动作的 sql_exec 类型走这里）。"""
    from app.services.data_plane.execute_service import (
        ExecuteBlocked, ExecuteRequest, ExecuteService,
    )

    try:
        result = ExecuteService(db).execute(ExecuteRequest(
            asset_id=asset_id,
            sql=sql,
            params=params or {},
            purpose="action.sql",
        ))
    except ExecuteBlocked as e:
        return {"ok": False, "message": f"执行被拒绝：{e.reason} {e.detail or ''}".strip()}
    except Exception as e:  # noqa: BLE001
        logger.warning("action sql failed: %s", e)
        return {"ok": False, "message": f"执行失败：{e}"}

    return {
        "ok": True,
        "message": "SQL 已执行",
        "output": {
            "columns": result.columns,
            "rows": result.rows,
            "rowsAffected": getattr(result, "rows_returned", None),
        },
    }
