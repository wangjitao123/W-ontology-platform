"""把 Doris 真实业务表建成元枢对象（一次性建档脚本）。

建两个对象：
    ContractVehicle / 合同车辆   ← dwh_dwd.dwd_crm_contract_vehicle_di
    FinanceMonth    / 财务月报   ← dwh_dws.dws_cdep_financial_indicator_month

并配置对象↔指标绑定：
    FinanceMonth.period        → cube finance_summary
    ContractVehicle.contract_code → cube contract_vehicle

可重复执行：已存在则跳过创建，只在缺失时补齐。
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.database import SessionLocal
from app.models.asset import Asset
from app.models.entity import OntologyEntity, EntityAttribute
from app.models.object_binding import ObjectBinding
from app.models.object_metric_binding import ObjectMetricBinding
from app.models.connection import Connection

DWD_CONN = "doris-dwh"
DWS_CONN = "doris-dws"

def _obj_config(spec: dict) -> dict:
    """对象类型的 config_json：主键 + 与 cube 对齐的过滤条件。"""
    cfg: dict = {"primary_key": spec["pk"]}
    if spec.get("filter"):
        cfg["filter_expr"] = spec["filter"]
    if spec.get("slice_label"):
        cfg["slice_label"] = spec["slice_label"]
    return cfg


OBJECTS = [
    {
        "entity_id": "ContractVehicle",
        "name_cn": "合同车辆",
        "tier": 2,
        "desc": "CRM 合同车辆明细（一合同×产品组合一行）",
        "connection": DWD_CONN,
        "schema": "dwh_dwd",
        "table": "dwd_crm_contract_vehicle_di",
        "pk": ["contract_code", "product_portfolio_id"],
        # 与 cube contract_vehicle 的 WHERE 保持一致，否则对象存在但指标为 0
        "filter": "contract_deleted = 0 AND sign_state = 4",
        "attrs": [
            ("contract_code", "string", "合同编号（复合主键之一）", True),
            ("product_portfolio_id", "integer", "产品组合 ID（复合主键之一）", True),
            ("contract_title", "string", "合同标题", False),
            ("number", "string", "合同流水号", False),
            ("sign_time", "datetime", "签约时间", False),
            ("approve_state_str", "string", "审批状态", False),
            ("actual_amount", "decimal", "合同实际金额", False),
            ("type_str", "string", "合同类型", False),
            ("sales_org", "string", "销售组织", False),
            ("factory", "string", "工厂", False),
            ("principal_name", "string", "负责人", False),
        ],
        "metric_bindings": [
            {
                "label": "本合同指标",
                "cube": "contract_vehicle",
                "key_column": "contract_code",
                "metrics": ["contract_count", "contract_vehicle_count"],
            },
        ],
    },
    {
        "entity_id": "FinanceMonth",
        "name_cn": "财务月报",
        "tier": 3,
        "desc": "财务指标月度汇总（一行=一个报表期间）",
        "connection": DWS_CONN,
        "schema": "dwh_dws",
        "table": "dws_cdep_financial_indicator_month",
        "pk": ["period"],
        "attrs": [
            ("period", "string", "报表期间（对象主键，如 2026-07）", True),
            ("year", "integer", "年份", False),
            ("month", "integer", "月份", False),
            ("total_assets", "decimal", "资产总额", False),
            ("total_liabilities", "decimal", "负债总额", False),
            ("revenue_month", "decimal", "本月营业收入", False),
            ("operating_cost_month", "decimal", "本月营业成本", False),
            ("cash", "decimal", "货币资金", False),
            ("inventory", "decimal", "存货", False),
            ("accounts_receivable", "decimal", "应收账款", False),
        ],
        "metric_bindings": [
            {
                "label": "本月财务指标（来自 Cube）",
                "cube": "finance_summary",
                "key_column": "period",
                "metrics": [
                    "total_assets", "total_liabilities", "debt_ratio",
                    "revenue_month", "net_profit_month", "current_ratio",
                ],
            },
        ],
    },
]


def main() -> int:
    db = SessionLocal()
    try:
        conns = {c.name: c for c in db.query(Connection).all()}
        for key in (DWD_CONN, DWS_CONN):
            if key not in conns:
                print(f"缺少连接 {key}，请先创建")
                return 1

        for spec in OBJECTS:
            conn = conns[spec["connection"]]
            eid = spec["entity_id"]

            # ── 资产 ──
            asset = db.query(Asset).filter(Asset.alias == f"{eid}_asset").first()
            if asset is None:
                asset = Asset(
                    name=spec["table"],
                    alias=f"{eid}_asset",
                    description=spec["desc"],
                    kind="table",
                    connection_id=conn.id,
                    locator={"schema": spec["schema"], "table": spec["table"]},
                    primary_key=spec["pk"],
                    domain="doris",
                    tags=["doris", "real"],
                )
                db.add(asset)
                db.flush()
                print(f"  资产已建: {asset.name}")
            else:
                print(f"  资产已存在: {asset.name}")

            # ── 对象类型 ──
            entity = db.get(OntologyEntity, eid)
            if entity is None:
                entity = OntologyEntity(
                    id=eid,
                    name=eid,
                    name_cn=spec["name_cn"],
                    tier=spec["tier"],
                    description=spec["desc"],
                    config_json=_obj_config(spec),
                )
                db.add(entity)
                db.flush()
                print(f"  对象已建: {eid} ({spec['name_cn']})")
            else:
                # 已存在：确保主键配置是最新的（复合键需要列表）
                entity.config_json = _obj_config(spec)
                print(f"  对象已存在: {eid}（主键配置已刷新为 {spec['pk']}）")

            # ── 属性（缺则补） ──
            existing = {a.name for a in entity.attributes}
            added = 0
            for name, typ, desc, required in spec["attrs"]:
                if name in existing:
                    continue
                db.add(EntityAttribute(
                    entity_id=eid,
                    name=name,
                    type=typ,
                    description=desc,
                    required=required,
                    source_table=spec["table"],
                    source_field=name,
                    data_status="confirmed",
                ))
                added += 1
            if added:
                db.flush()
                print(f"     属性新增 {added} 个")

            # ── 主绑定 ──
            binding = (
                db.query(ObjectBinding)
                .filter(ObjectBinding.object_type_id == eid, ObjectBinding.role == "primary")
                .first()
            )
            if binding is None:
                db.add(ObjectBinding(
                    object_type_id=eid,
                    asset_id=asset.id,
                    role="primary",
                    id_column=spec["pk"][0],
                    field_mappings=[
                        {"attribute": n, "column": n} for n, *_ in spec["attrs"]
                    ],
                ))
                print("     主绑定已建")
            else:
                print("     主绑定已存在")

            # ── 指标绑定 ──
            for mb in spec["metric_bindings"]:
                exists = (
                    db.query(ObjectMetricBinding)
                    .filter(
                        ObjectMetricBinding.object_type_id == eid,
                        ObjectMetricBinding.cube_name == mb["cube"],
                        ObjectMetricBinding.key_column == mb["key_column"],
                    )
                    .first()
                )
                if exists is None:
                    db.add(ObjectMetricBinding(
                        object_type_id=eid,
                        label=mb["label"],
                        cube_name=mb["cube"],
                        key_column=mb["key_column"],
                        filter_member=f"{mb['cube']}.{mb['key_column']}",
                        metric_names=mb["metrics"],
                        description=f"按 {mb['key_column']} 过滤 Cube {mb['cube']}",
                    ))
                    print(f"     指标绑定已建: {mb['cube']} ← {mb['key_column']}")

        db.commit()
        print("\n完成")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
