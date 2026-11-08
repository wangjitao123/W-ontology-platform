"""建真正的实体层：把"有稳定身份的业务主语"从事实表里反推出来。

## 为什么要这一步

原来的 11 个对象类型是从 cube 的维度机械提取的，其中 7 个的主键里带日期
（`record_date` / `data_date` / `period` / `production_month`）。那意味着同一个
业务实体每天变成一个新对象——这不是对象，是**带时间戳的快照行**。

实体层要修的就是这件事：对象的主语必须是客户、物料、供应商、工厂这种跨时间
身份不变的东西；库龄、余额、排产是它们的**时间序列属性**，不是独立对象。

## 反推的依据（本轮在 Doris 上实测）

    客户   kunnr        2,576 个   来源 2 张应收表
    物料   material_code 8,924 个   三张库龄表并集；跨表同名冲突 0 个
    供应商 supplier_code    11 个
    工厂   plant_code       19 个

物料"一名多码" 1,040 组不是脏数据，是 SAP 里同描述不同料号的正常现象，
所以**身份一律用码，名字只当标签**（这正是本条决定的核心）。

## 边界（必须知道）

反推出来的是"在数仓里出现过的实体"，不是权威主数据：
客户只有"有应收账款的客户"，物料只有"有过库存/排产的物料"。
要全量主数据必须接业务系统，这里做不到。

## 幂等

可重复执行。已存在的类型/资产/属性/绑定会跳过；FinanceMonth 的清理也幂等。
"""
from __future__ import annotations

import io
import sys

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.database import SessionLocal  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.entity import EntityAttribute, OntologyEntity  # noqa: E402
from app.models.object_binding import ObjectBinding  # noqa: E402
from app.models.object_index import ObjectIndexEntry, ObjectIndexSyncState  # noqa: E402
from app.models.object_metric_binding import ObjectMetricBinding  # noqa: E402

TYPE_MAP = {
    "VARCHAR": "string", "CHAR": "string", "TEXT": "string", "STRING": "string",
    "INT": "integer", "BIGINT": "integer", "SMALLINT": "integer", "TINYINT": "integer",
    "DECIMAL": "decimal", "DOUBLE": "decimal", "FLOAT": "decimal",
    "DATETIME": "datetime", "DATE": "datetime", "TIMESTAMP": "datetime",
    "BOOLEAN": "boolean",
}

# ── 实体定义 ──────────────────────────────────────────────
# source_sql 全部经过实测：行数与体检报告一致
ENTITIES = [
    {
        "id": "Customer", "cn": "客户", "tier": 1,
        "pk": "customer_code",
        "deps": ["dwd_sap_finance_open_items_30ma",
                 "dwd_sap_finance_open_items_tmp_30ma"],
        "sql": """
            SELECT kunnr AS customer_code,
                   MAX(name1) AS customer_name
            FROM (
                SELECT kunnr, name1 FROM dwh_dwd.dwd_sap_finance_open_items_30ma
                 WHERE kunnr IS NOT NULL AND kunnr <> ''
                UNION ALL
                SELECT kunnr, name1 FROM dwh_dwd.dwd_sap_finance_open_items_tmp_30ma
                 WHERE kunnr IS NOT NULL AND kunnr <> ''
            ) t
            GROUP BY kunnr
        """,
        "attrs": ["customer_code", "customer_name"],
        "max_rows": 30000,
        "note": "有应收账款的客户（2576 个）；全量客户主数据不在数仓里",
    },
    {
        "id": "Material", "cn": "物料", "tier": 1,
        "pk": "material_code",
        "deps": ["dwd_sap_inv_wms_aging_detail_1da",
                 "dwd_sap_inv_sap_b2b_aging_detail_1da",
                 "dwd_sap_inv_sap_vehicle_aging_detail_1da"],
        "sql": """
            SELECT material_code,
                   MAX(material_name) AS material_name
            FROM (
                SELECT material_code, material_name
                  FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da
                 WHERE material_code IS NOT NULL AND material_code <> ''
                UNION ALL
                SELECT material_code, material_name
                  FROM dwh_dwd.dwd_sap_inv_sap_b2b_aging_detail_1da
                 WHERE material_code IS NOT NULL AND material_code <> ''
                UNION ALL
                SELECT material_code, material_name
                  FROM dwh_dwd.dwd_sap_inv_sap_vehicle_aging_detail_1da
                 WHERE material_code IS NOT NULL AND material_code <> ''
            ) t
            GROUP BY material_code
        """,
        "attrs": ["material_code", "material_name"],
        "max_rows": 30000,
        "note": "出现过库存的物料（8924 个）；身份用码，名字有 1040 组一对多",
    },
    {
        "id": "Supplier", "cn": "供应商", "tier": 2,
        "pk": "supplier_code",
        "deps": ["dwd_sap_inv_sap_b2b_aging_detail_1da"],
        "sql": """
            SELECT supplier_code,
                   MAX(supplier_name) AS supplier_name
              FROM dwh_dwd.dwd_sap_inv_sap_b2b_aging_detail_1da
             WHERE supplier_code IS NOT NULL AND supplier_code <> ''
             GROUP BY supplier_code
        """,
        "attrs": ["supplier_code", "supplier_name"],
        "max_rows": 2000,
        "note": "B2件库龄里出现过的供应商（11 个）",
    },
    {
        "id": "Plant", "cn": "工厂", "tier": 2,
        "pk": "plant_code",
        "deps": ["dwd_sap_inv_wms_aging_detail_1da",
                 "dwd_sap_inv_sap_b2b_aging_detail_1da",
                 "dwd_sap_inv_sap_vehicle_aging_detail_1da"],
        "sql": """
            SELECT plant_code,
                   MAX(plant_name) AS plant_name
            FROM (
                SELECT plant_code, plant_name
                  FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da
                 WHERE plant_code IS NOT NULL AND plant_code <> ''
                UNION ALL
                SELECT plant_code, plant_name
                  FROM dwh_dwd.dwd_sap_inv_sap_b2b_aging_detail_1da
                 WHERE plant_code IS NOT NULL AND plant_code <> ''
                UNION ALL
                SELECT plant_code, plant_name
                  FROM dwh_dwd.dwd_sap_inv_sap_vehicle_aging_detail_1da
                 WHERE plant_code IS NOT NULL AND plant_code <> ''
            ) t
            GROUP BY plant_code
        """,
        "attrs": ["plant_code", "plant_name"],
        "max_rows": 2000,
        "note": "库存相关工厂（19 个）",
    },
]

# ── object_kind 分类 ──────────────────────────────────────
# entity   : 身份跨时间稳定，可被操作、可被关联
# snapshot : 事实快照，身份里带日期；作为实体的时间序列存在
# report   : 聚合报表，不该是对象
KIND = {
    "AbroadContract": "entity",
    "AbroadDelivery": "entity",
    "ArOpenItem": "entity",
    "ContractVehicle": "entity",
    "Customer": "entity",
    "Material": "entity",
    "Supplier": "entity",
    "Plant": "entity",
    "B2bAging": "snapshot",
    "WmsAging": "snapshot",
    "VehicleAging": "snapshot",
    "FundBankBalance": "snapshot",
    "FundFinancingMaturity": "snapshot",
    "OtdSchedule": "snapshot",
    "FinanceMonth": "report",       # 会被清理掉
}

KIND_LABEL = {
    "entity": "实体（身份稳定，可被操作）",
    "snapshot": "快照（事实行，按时间序列存在）",
    "report": "报表（聚合结果，不是对象）",
}

# 指标绑定从快照迁到实体：物料的三个口径都该挂到物料对象上
# （同一列名 material_code，所以只是换挂载点，口径不变）
REPOINT_BINDINGS = [
    {"from": "WmsAging", "to": "Material", "key": "material_code"},
    {"from": "B2bAging", "to": "Material", "key": "material_code"},
    {"from": "VehicleAging", "to": "Material", "key": "material_code"},
]

# 要清理的报表型对象（用户已确认：指标层已覆盖）
DROP_TYPES = ["FinanceMonth"]


def main() -> int:
    from app.models.connection import Connection

    db = SessionLocal()
    try:
        conns = {c.name: c for c in db.query(Connection).all()}
        conn = conns.get("doris-dwh")
        if conn is None:
            print("  缺少连接 doris-dwh")
            return 1

        # ── 1) 建 4 个实体 ──
        print("=== 1) 建实体层 ===")
        for spec in ENTITIES:
            eid = spec["id"]
            asset = db.query(Asset).filter(Asset.alias == f"{eid}_asset").first()
            if asset is None:
                asset = Asset(
                    name=f"{eid}(实体)",
                    alias=f"{eid}_asset",
                    kind="sql_view",
                    description=f"{spec['cn']}实体：{spec['note']}",
                    connection_id=conn.id,
                    # sql_view 的白名单看 dependencies；table 仅用于展示
                    locator={
                        "schema": "dwh_dwd",
                        "table": spec["deps"][0],
                        "dependencies": spec["deps"],
                    },
                    primary_key=[spec["pk"]],
                    domain="doris",
                    tags=["doris", "entity"],
                )
                db.add(asset)
                db.flush()
            else:
                asset.locator = {
                    "schema": "dwh_dwd",
                    "table": spec["deps"][0],
                    "dependencies": spec["deps"],
                }

            cfg = {
                "primary_key": spec["pk"],
                "object_kind": "entity",
                "source_sql": " ".join(spec["sql"].split()),
                "index_max_rows": spec["max_rows"],
                "entity_note": spec["note"],
            }

            entity = db.get(OntologyEntity, eid)
            if entity is None:
                entity = OntologyEntity(
                    id=eid, name=eid, name_cn=spec["cn"], tier=spec["tier"],
                    description=f"{spec['cn']}（实体层）：{spec['note']}",
                    config_json=cfg,
                )
                db.add(entity)
                db.flush()
                print(f"  新建 {eid:10} {spec['cn']}")
            else:
                entity.config_json = cfg
                print(f"  更新 {eid:10} {spec['cn']}")

            existing = {a.name for a in entity.attributes}
            for col in spec["attrs"]:
                if col in existing:
                    continue
                db.add(EntityAttribute(
                    entity_id=eid, name=col,
                    type="string",
                    description="",
                    required=(col == spec["pk"]),
                    source_table=spec["deps"][0],
                    source_field=col,
                    data_status="confirmed",
                ))
            db.flush()

            binding = db.query(ObjectBinding).filter(
                ObjectBinding.object_type_id == eid,
                ObjectBinding.role == "primary",
            ).first()
            if binding is None:
                db.add(ObjectBinding(
                    object_type_id=eid, asset_id=asset.id, role="primary",
                ))
        db.commit()

        # ── 2) 给所有类型打 object_kind ──
        print("=== 2) 标注 object_kind ===")
        counts = {"entity": 0, "snapshot": 0, "report": 0, "unknown": 0}
        for e in db.query(OntologyEntity).all():
            kind = KIND.get(e.id)
            if kind is None:
                counts["unknown"] += 1
                continue
            cfg = dict(e.config_json or {})
            cfg["object_kind"] = kind
            cfg["object_kind_label"] = KIND_LABEL[kind]
            e.config_json = cfg
            counts[kind] += 1
        db.commit()
        print(f"  entity={counts['entity']}  snapshot={counts['snapshot']}  "
              f"report={counts['report']}  未分类={counts['unknown']}")

        # ── 3) 指标绑定从快照迁到实体 ──
        print("=== 3) 指标绑定迁移（快照 → 实体）===")
        for move in REPOINT_BINDINGS:
            src = db.query(ObjectMetricBinding).filter(
                ObjectMetricBinding.object_type_id == move["from"],
            ).all()
            for b in src:
                exists = db.query(ObjectMetricBinding).filter(
                    ObjectMetricBinding.object_type_id == move["to"],
                    ObjectMetricBinding.cube_name == b.cube_name,
                ).first()
                if exists:
                    db.delete(b)
                    continue
                b.object_type_id = move["to"]
                b.key_column = move["key"]
                print(f"  {move['from']:16} → {move['to']:10} cube={b.cube_name}")
        db.commit()

        # ── 4) 清理报表型对象 ──
        print("=== 4) 清理 report 型对象 ===")
        for eid in DROP_TYPES:
            e = db.get(OntologyEntity, eid)
            if e is None:
                print(f"  {eid}: 已不存在")
                continue
            db.query(EntityAttribute).filter(EntityAttribute.entity_id == eid).delete()
            db.query(ObjectBinding).filter(ObjectBinding.object_type_id == eid).delete()
            db.query(ObjectMetricBinding).filter(
                ObjectMetricBinding.object_type_id == eid).delete()
            db.query(ObjectIndexEntry).filter(
                ObjectIndexEntry.object_type_id == eid).delete()
            db.query(ObjectIndexSyncState).filter(
                ObjectIndexSyncState.object_type_id == eid).delete()
            asset = db.query(Asset).filter(Asset.alias == f"{eid}_asset").first()
            if asset:
                db.delete(asset)
            db.delete(e)
            print(f"  删除 {eid}（含资产/属性/绑定/索引）")
        db.commit()

        # ── 5) 清理 Postgres 对象存储里的旧数据 ──
        print("=== 5) 同步对象存储 ===")
        from app.services.object_store import get_object_store

        store = get_object_store()
        if store is None:
            print("  对象存储未启用，跳过")
        else:
            for eid in DROP_TYPES:
                n = store.delete_type(eid)
                print(f"  清理 {eid}: {n} 行")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
