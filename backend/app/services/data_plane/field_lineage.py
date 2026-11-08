"""字段级血缘 —— 从"指标"一路追到"Doris 的哪张表哪一列"。

## 链路

```
指标 metric:finance_summary.total_assets
   │  uses   （cube 模型里 measure 的 SQL 表达式引用了哪些列）
   ▼
物理列 column:dws_cdep_financial_indicator_month.total_assets
   │  belongs_to            │  maps_to
   ▼                        ▼
资产 asset（表）        对象属性 attribute:FinanceMonth.total_assets
                            │  belongs_to
                            ▼
                        对象类型 object_type
```

## 列是怎么定位的

cube 的 YAML 给了两条线索：

1. ``meta.atomic`` —— 生成脚本已经把"原子列名"写好了（``total_assets / 100000000``
   的 atomic 就是 ``total_assets``），优先用它
2. 没有 ``meta.atomic`` 时解析表达式里的标识符，再拿 base 表的真实列名校验——
   只保留真实存在的列，避免把 ``CONCAT``/``STR_TO_DATE`` 这类函数名当列

所以这里必须知道 base 表有哪些列，直接查 Doris 的 ``information_schema.columns``
（带进程内缓存，重建时不至于每次几百张表全扫一遍）。

## 为什么单独一个模块

`lineage_builder` 管"配置事实"（谁绑了谁），这里管"SQL 事实"（表达式读了哪些列）。
两者都会写 `lineage_edges`，但数据源和解析成本不同，混在一起会很难维护。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.lineage_edge import LineageEdge

logger = logging.getLogger(__name__)

FIELD_RELATIONS = (
    "uses",            # metric → column
    "belongs_to",      # column → asset / attribute → object_type
    "maps_to",         # column → attribute
    "bound_to",        # metric → object_type（对象指标绑定）
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FROM = re.compile(r"\bFROM\s+([A-Za-z0-9_.]+)", re.IGNORECASE)

# Doris 列清单缓存：{table_lower: set(columns)}，重建时复用
_COLUMNS_CACHE: dict[str, set[str]] = {}
_COLUMNS_AT: float = 0.0
_COLUMNS_TTL = 600.0

# cube YAML 缓存
_CUBES_CACHE: list[dict[str, Any]] | None = None
_CUBES_AT: float = 0.0
_CUBES_TTL = 600.0


# ── cube 模型解析 ──────────────────────────────────────────

def load_cubes(model_dir: Path | None = None) -> list[dict[str, Any]]:
    """读 cube 模型，返回 [{cube, table, items:[{kind,name,title,sql,atomic,source_table}]}]。"""
    global _CUBES_CACHE, _CUBES_AT

    now = time.time()
    if _CUBES_CACHE is not None and now - _CUBES_AT < _CUBES_TTL:
        return _CUBES_CACHE

    from app.config import settings

    d = Path(model_dir or settings.CUBE_MODEL_DIR or "")
    if not d.is_dir():
        logger.warning("字段血缘: cube 模型目录不存在 %s", d)
        _CUBES_CACHE, _CUBES_AT = [], now
        return []

    import yaml

    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("字段血缘: 解析 %s 失败: %s", path.name, e)
            continue

        for cube in doc.get("cubes") or []:
            name = cube.get("name")
            raw_sql = " ".join(str(cube.get("sql") or "").split())
            m = _FROM.search(raw_sql)
            table = (m.group(1).split(".")[-1].lower() if m else None)
            if not name or not table:
                continue

            items: list[dict[str, Any]] = []
            for kind, key in (("measure", "measures"), ("dimension", "dimensions")):
                for item in cube.get(key) or []:
                    meta = item.get("meta") or {}
                    items.append({
                        "kind": kind,
                        "name": item.get("name"),
                        "title": item.get("title") or item.get("name"),
                        "sql": " ".join(str(item.get("sql") or "").split()),
                        "atomic": meta.get("atomic"),
                        "source_table": (str(meta.get("source_table") or "")
                                         .split(".")[-1].lower() or None),
                    })
            out.append({"cube": name, "table": table, "items": items})

    _CUBES_CACHE, _CUBES_AT = out, now
    return out


def metric_titles() -> dict[str, str]:
    """{cube.item: 中文标题}，给节点补标签。"""
    return {
        f"{c['cube']}.{i['name']}": str(i.get("title") or i["name"])
        for c in load_cubes() for i in c["items"] if i.get("name")
    }


def resolve_columns(item: dict[str, Any], table_cols: set[str]) -> list[str]:
    """一条指标/维度引用了哪些物理列。

    优先 ``meta.atomic``（生成脚本已经标好的原子列），否则解析表达式并只保留
    表里真实存在的标识符——这样 ``STR_TO_DATE``、``CONCAT`` 这类函数名不会被
    误当成列。
    """
    atomic = (item.get("atomic") or "").strip()
    if atomic and atomic.lower() in table_cols:
        return [atomic.lower()]

    expr = item.get("sql") or ""
    seen: list[str] = []
    for ident in _IDENT.findall(expr):
        low = ident.lower()
        if low in table_cols and low not in seen:
            seen.append(low)
    return seen


# ── Doris 列清单 ──────────────────────────────────────────

def doris_columns(db: Session, *, force: bool = False) -> dict[str, set[str]]:
    """{表名小写: 列名集合}，来自 Doris 的 information_schema。

    重建一次要覆盖十来个 cube 的 base 表，所以整库拉一次比逐表查便宜。
    走 ConnectionService.with_conn()，不自己取密码——凭据在 vault 里，
    调用方不该看到明文，也不该绕过连接池。
    """
    global _COLUMNS_CACHE, _COLUMNS_AT

    now = time.time()
    if not force and _COLUMNS_CACHE and now - _COLUMNS_AT < _COLUMNS_TTL:
        return _COLUMNS_CACHE

    from app.models.connection import Connection
    from app.services.data_plane.connection_service import ConnectionService

    conn_row = db.query(Connection).filter(Connection.name == "doris-dwh").first()
    if conn_row is None:
        logger.warning("字段血缘: 找不到 doris-dwh 连接，无法取列清单")
        return _COLUMNS_CACHE or {}

    try:
        svc = ConnectionService(db)
        with svc.with_conn(conn_row.id) as raw:
            cur = raw.cursor()
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema IN ('dwh_dwd','dwh_dws','dwh_ads')"
            )
            out: dict[str, set[str]] = {}
            for t, c in cur.fetchall():
                out.setdefault(str(t).lower(), set()).add(str(c).lower())
            cur.close()
        _COLUMNS_CACHE, _COLUMNS_AT = out, now
        logger.info("字段血缘: 取到 %d 张表的列清单", len(out))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("字段血缘: 拉 Doris 列清单失败: %s", e)
        return _COLUMNS_CACHE or {}


# ── 写边 ──────────────────────────────────────────────────

def _edge(
    db: Session,
    *, sk: str, si: str, tk: str, ti: str, relation: str, weight: int = 1,
) -> bool:
    """存在则不动（幂等），不存在则插入。返回是否新建。"""
    exists = (
        db.query(LineageEdge.id)
        .filter(
            LineageEdge.source_kind == sk, LineageEdge.source_id == si,
            LineageEdge.target_kind == tk, LineageEdge.target_id == ti,
            LineageEdge.relation == relation,
        )
        .first()
    )
    if exists:
        return False
    db.add(LineageEdge(
        source_kind=sk, source_id=si, target_kind=tk, target_id=ti,
        relation=relation, via_module="field_lineage", weight=max(1, weight),
    ))
    return True


def build(db: Session) -> dict[str, Any]:
    """重建字段级血缘，返回统计。"""
    from app.models.entity import OntologyEntity
    from app.models.object_metric_binding import ObjectMetricBinding

    cols = doris_columns(db)

    # 表 → 资产（列节点要能连到它的表资产上）
    from app.models.asset import Asset

    asset_by_table: dict[str, str] = {}
    for a in db.query(Asset).all():
        t = (a.locator or {}).get("table")
        if t:
            asset_by_table.setdefault(str(t).lower(), a.id)

    # 列 → 对象属性（EntityAttribute 的 source_table/source_field）
    from app.models.entity import EntityAttribute

    attrs = (
        db.query(EntityAttribute)
        .filter(EntityAttribute.source_field.isnot(None))
        .all()
    )
    attr_by_col: dict[str, list[tuple[str, str]]] = {}
    for a in attrs:
        tbl = (a.source_table or "").lower()
        col = (a.source_field or "").lower()
        if tbl and col:
            attr_by_col.setdefault(f"{tbl}.{col}", []).append((a.entity_id, a.name))

    # 指标/维度的中文标题，写进边里做标签
    titles = metric_titles()

    created = 0
    stats = {"metric_column": 0, "column_asset": 0, "column_attribute": 0,
             "attribute_object": 0, "metric_object": 0}
    seen: set[tuple[str, str, str, str, str]] = set()

    def add(sk, si, tk, ti, relation, bucket):
        nonlocal created
        key = (sk, si, tk, ti, relation)
        if key in seen:
            return
        seen.add(key)
        if _edge(db, sk=sk, si=si, tk=tk, ti=ti, relation=relation):
            created += 1
        stats[bucket] += 1

    # 1) metric --uses--> column，并顺带 column --belongs_to--> asset
    cubes = load_cubes()
    for c in cubes:
        table = c["table"]
        table_cols = cols.get(table) or set()
        if not table_cols:
            logger.debug("字段血缘: 表 %s 没有列信息，跳过", table)
            continue
        for item in c["items"]:
            if not item.get("name"):
                continue
            mid = f"{c['cube']}.{item['name']}"
            src_table = item.get("source_table") or table
            src_cols = cols.get(src_table) or table_cols
            for col in resolve_columns(item, src_cols):
                cid = f"{src_table}.{col}"
                add("metric", mid, "column", cid, "uses", "metric_column")
                aid = asset_by_table.get(src_table)
                if aid:
                    add("column", cid, "asset", aid, "belongs_to", "column_asset")

    # 2) column --maps_to--> attribute，attribute --belongs_to--> object_type
    entity_ids = {e.id for e in db.query(OntologyEntity).all()}
    for colkey, owners in attr_by_col.items():
        for entity_id, attr_name in owners:
            if entity_id not in entity_ids:
                continue
            attr_id = f"{entity_id}.{attr_name}"
            add("column", colkey, "attribute", attr_id, "maps_to", "column_attribute")
            add("attribute", attr_id, "object_type", entity_id, "belongs_to",
                "attribute_object")

    # 3) metric --bound_to--> object_type（对象指标绑定里列出的指标）
    for b in db.query(ObjectMetricBinding).all():
        if b.object_type_id not in entity_ids:
            continue
        for mname in (b.metric_names or []):
            mid = f"{b.cube_name}.{mname}"
            add("metric", mid, "object_type", b.object_type_id, "bound_to",
                "metric_object")

    # 4) 失效的字段级边：置 weight=0
    now = time.time()
    stale = (
        db.query(LineageEdge)
        .filter(LineageEdge.relation.in_(FIELD_RELATIONS))
        .all()
    )
    from datetime import datetime
    ts = datetime.utcnow()
    deprecated = 0
    for e in stale:
        k = (e.source_kind, e.source_id, e.target_kind, e.target_id, e.relation)
        if k not in seen and (e.weight or 0) > 0:
            e.weight = 0
            e.last_seen_at = ts
            deprecated += 1

    db.commit()
    active = (
        db.query(func.count(LineageEdge.id))
        .filter(LineageEdge.weight > 0)
        .scalar() or 0
    )
    logger.info("字段血缘: 新建 %d 条，当前有效边 %d 条", created, active)
    return {**stats, "created": created, "deprecated": deprecated,
            "activeEdges": int(active), "metricTitles": len(titles)}
