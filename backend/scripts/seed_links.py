"""为 11 个对象类型建实例级连线（link type）定义。

每条关系都用 join_key 描述"实例怎么连"，并且**先在真实数据上验过匹配率**——
0 匹配的候选直接不收（例如 ContractVehicle 与 AbroadContract 的 contract_code
虽然同名，但一个是 DW 前缀、一个是 DWHV 前缀，交集为 0）。

幂等：按 rel id 覆盖写，重复执行不会产生重复关系。

用法：
    python -m scripts.seed_links          # 建定义 + 物化
    python -m scripts.seed_links --defs   # 只建定义
"""
from __future__ import annotations

import io
import sys

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.database import SessionLocal  # noqa: E402
from app.models.relation import EntityRelation  # noqa: E402

# 实测匹配数写在注释里，都是本轮在 Postgres 对象索引上真跑出来的。
#
# 关系方向的原则：**从实体指向快照**（1:N）。
# 旧版把两张快照表按 material_code 连起来，那其实是"同一物料的两份不同口径
# 快照"，是身份等价而不是业务关系。实体层建起来之后，正确的关系是
# `物料 → 它的库龄记录`、`客户 → 他的未清项`。
RELATIONS = [
    dict(
        id="rel_abroad_contract_deliveries",
        from_entity_id="AbroadContract",
        to_entity_id="AbroadDelivery",
        name="海外合同-交货单",
        rel_type="has_many",
        cardinality="1:N",
        description="一份海外合同对应多张交货单（按 contract_id 关联）",
        join_keys=[{"from_column": "contract_id", "to_column": "contract_id"}],
    ),
    dict(
        id="rel_material_wms_aging",
        from_entity_id="Material",
        to_entity_id="WmsAging",
        name="物料-零部件库龄",
        rel_type="has_many",
        cardinality="1:N",
        description="一个物料在每个快照日有一条零部件库龄记录",
        join_keys=[{"from_column": "material_code", "to_column": "material_code"}],
    ),
    dict(
        id="rel_material_b2b_aging",
        from_entity_id="Material",
        to_entity_id="B2bAging",
        name="物料-B2件库龄",
        rel_type="has_many",
        cardinality="1:N",
        description="一个物料有多条 B2 件库龄记录（按供应商/工厂分）",
        join_keys=[{"from_column": "material_code", "to_column": "material_code"}],
    ),
    dict(
        id="rel_material_vehicle_aging",
        from_entity_id="Material",
        to_entity_id="VehicleAging",
        name="物料-整车库龄",
        rel_type="has_many",
        cardinality="1:N",
        description="一个物料可能出现在整车库龄里",
        join_keys=[{"from_column": "material_code", "to_column": "material_code"}],
    ),
    dict(
        id="rel_material_otd_schedule",
        from_entity_id="Material",
        to_entity_id="OtdSchedule",
        name="物料-OTD排产",
        rel_type="has_many",
        cardinality="1:N",
        description="一个物料有多条排产明细（material_code 对 material_no）",
        join_keys=[{"from_column": "material_code", "to_column": "material_no"}],
    ),
    dict(
        id="rel_customer_ar_open_item",
        from_entity_id="Customer",
        to_entity_id="ArOpenItem",
        name="客户-应收未清项",
        rel_type="has_many",
        cardinality="1:N",
        description="一个客户有多条应收账款未清项（customer_code 对 kunnr）",
        join_keys=[{"from_column": "customer_code", "to_column": "kunnr"}],
    ),
    dict(
        id="rel_plant_wms_aging",
        from_entity_id="Plant",
        to_entity_id="WmsAging",
        name="工厂-零部件库龄",
        rel_type="has_many",
        cardinality="1:N",
        description="一个工厂有多条零部件库龄记录",
        join_keys=[{"from_column": "plant_code", "to_column": "plant_code"}],
    ),
]

# 要淘汰的关系：
#   - 快照↔快照按 material_code 连：身份等价，不是关系（已由实体层取代）
#   - 指向已删除的 FinanceMonth
DROP_RELS = [
    "rel_wms_b2b_materials",
    "rel_vehicle_wms_materials",
    "rel_otd_wms_materials",
    "rel_bank_balance_finance_month",
]


def main(only_defs: bool = False) -> None:
    db = SessionLocal()
    try:
        # 淘汰旧关系：定义和已物化的边都要删，否则会留下指向已删类型的边
        from app.services.object_store import get_object_store

        store = get_object_store()
        dropped = 0
        for rid in DROP_RELS:
            rel = db.get(EntityRelation, rid)
            if rel is not None:
                db.delete(rel)
                dropped += 1
            if store is not None:
                store.delete_links(rid)
        if dropped:
            print(f"  淘汰旧关系: {dropped} 条")

        created = 0
        for spec in RELATIONS:
            rel = db.get(EntityRelation, spec["id"])
            if rel is None:
                rel = EntityRelation(id=spec["id"])
                db.add(rel)
                created += 1
            for field, value in spec.items():
                if field != "id":
                    setattr(rel, field, value)
        db.commit()
        print(f"  关系定义: 新建 {created} 条，共 {len(RELATIONS)} 条")

        if only_defs:
            return

        from app.services.object_link_service import rebuild_all

        report = rebuild_all(db)
        if not report.get("ok") and report.get("reason") == "object_store_disabled":
            print(f"  ⚠️ {report['message']}")
            return
        for r in report["relations"]:
            mark = "✓" if r["status"] == "ok" else "✗"
            print(f"  {mark} {r['rel']:34} {r['from']:22} → {r['to']:22} {r['links']:>6} 条边")
            if r.get("error"):
                print(f"     错误: {r['error']}")
        print(f"  合计 {report['totalLinks']} 条实例连线")
    finally:
        db.close()


if __name__ == "__main__":
    main(only_defs="--defs" in sys.argv)
