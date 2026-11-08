"""把 Cube 背后的 9 张真实业务表建成元枢对象（一次性建档脚本）。

原则：
    1. **只建"有行级身份"的 cube** —— 汇总/排名/分布类 cube 保持纯指标。
    2. **大表必须收窄** —— wms_detail 179 万行、ar_open_items 948 万行，
       直接把整表当对象集合没有意义，用 filter_expr 限定到最新快照 / 本月。
    3. **主键必须唯一** —— 逐表验证过 COUNT(*) == COUNT(DISTINCT key)；
       个别有真重复的（ar_open_items 61 行）由同步跳过并告警。
    4. **列名以源表为准** —— 脚本会先查 information_schema，只创建真实存在的列，
       避免手写列名写错。
    5. **对象级指标只在语义成立时绑定** —— cube 的维度里必须有对象的主键列，
       否则宁可不绑，也不做"看起来有指标"的假绑定。

可重复执行：已存在的对象/资产/绑定会跳过。
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import os

import pymysql

from app.database import SessionLocal
from app.models.asset import Asset
from app.models.entity import OntologyEntity, EntityAttribute
from app.models.object_binding import ObjectBinding
from app.models.object_metric_binding import ObjectMetricBinding
from app.models.connection import Connection

# 数仓连接信息一律走环境变量，不写死在代码里。
# 之前这里把生产凭据硬编码进了仓库，已移除。

DORIS = dict(
    host=os.environ.get("DORIS_HOST", ""),
    port=int(os.environ.get("DORIS_PORT", "9030")),
    user=os.environ.get("DORIS_USER", ""),
    password=os.environ.get("DORIS_PASSWORD", ""),
)

if not DORIS["host"] or not DORIS["user"]:
    raise SystemExit(
        "缺少数仓连接配置。请先设置环境变量：\n"
        "  DORIS_HOST=...  DORIS_PORT=9030  DORIS_USER=...  DORIS_PASSWORD=...\n"
        "（见 backend/.env.example）"
    )

SPECS = [
    # ── 库存 ─────────────────────────────────────────────
    {
        "id": "VehicleAging", "cn": "整车库龄", "tier": 2,
        "conn": "doris-dwh", "schema": "dwh_dwd",
        "table": "dwd_sap_inv_sap_vehicle_aging_detail_1da",
        "pk": ["record_date", "material_code", "plant_code", "warehouse_code"],
        "filter": "record_date=(SELECT MAX(record_date) FROM dwd_sap_inv_sap_vehicle_aging_detail_1da)",
        "slice": "仅最新快照日（源表 1.1 万行跨多个日期）",
        "max_rows": 5000,
        "attrs": ["record_date", "module_category", "plant_name", "warehouse_name",
                  "vehicle_model", "material_code", "material_name",
                  "total_stock_qty", "total_stock_value"],
        "metric": {"cube": "vehicle_detail", "key": "material_code",
                   "label": "本物料整车库龄", "member": "vehicle_detail.material_code",
                   "metrics": ["vehicle_total_qty", "vehicle_total_value", "vehicle_aging_gt_180_qty"]},
    },
    {
        "id": "WmsAging", "cn": "零部件库龄", "tier": 2,
        "conn": "doris-dwh", "schema": "dwh_dwd",
        "table": "dwd_sap_inv_wms_aging_detail_1da",
        "pk": ["record_date", "material_code", "plant_code", "warehouse_code"],
        "filter": "record_date=(SELECT MAX(record_date) FROM dwd_sap_inv_wms_aging_detail_1da)",
        "slice": "仅最新快照日（源表 179 万行跨多个日期）",
        "max_rows": 15000,
        "attrs": ["record_date", "module_category", "plant_name", "warehouse_name",
                  "material_code", "material_name", "total_stock_qty", "total_stock_value"],
        "metric": {"cube": "wms_detail", "key": "material_code",
                   "label": "本物料零部件库龄", "member": "wms_detail.material_code",
                   "metrics": ["material_total_qty", "wms_total_value", "wms_aging_gt_180_value"]},
    },
    {
        "id": "B2bAging", "cn": "B2件库龄", "tier": 2,
        "conn": "doris-dwh", "schema": "dwh_dwd",
        "table": "dwd_sap_inv_sap_b2b_aging_detail_1da",
        "pk": ["record_date", "material_code", "plant_code", "supplier_code"],
        "filter": None, "max_rows": 30000,
        "attrs": ["record_date", "module_category", "plant_name", "supplier_name",
                  "material_code", "material_name", "total_stock_qty", "total_stock_value"],
        "metric": {"cube": "b2b_detail", "key": "material_code",
                   "label": "本物料B2件库龄", "member": "b2b_detail.material_code",
                   "metrics": ["b2b_total_qty", "b2b_total_value"]},
    },
    # ── 销售 ─────────────────────────────────────────────
    {
        "id": "AbroadContract", "cn": "海外合同车辆", "tier": 2,
        "conn": "doris-dwh", "schema": "dwh_dwd",
        "table": "dwd_crm_abroad_contract_vehicle_di",
        "pk": ["contract_id", "goods_item_id"],
        "filter": "contract_deleted = 0 AND approve_state = 2", "max_rows": 5000,
        "attrs": ["contract_code", "contract_id", "goods_item_id", "customer_name",
                  "delivery_country_name", "contract_total_price", "contract_currency",
                  "principal_name"],
        "metric": None,   # cube 维度里没有合同标识，只能聚合，不做假绑定
    },
    {
        "id": "AbroadDelivery", "cn": "海外交货单", "tier": 2,
        "conn": "doris-dwh", "schema": "dwh_dwd",
        "table": "dwd_crm_abroad_delivery_vehicle_di",
        "pk": ["delivery_id"],
        "filter": "delivery_deleted = 0", "max_rows": 5000,
        "attrs": ["delivery_id", "contract_id", "vin", "quantity", "delivery_date",
                  "wms_delivery_no", "sap_delivery_no", "declaration_no"],
        "metric": None,
    },
    # ── 应收 ─────────────────────────────────────────────
    {
        "id": "ArOpenItem", "cn": "应收账款未清项", "tier": 2,
        "conn": "doris-dwh", "schema": "dwh_dwd",
        "table": "dwd_sap_finance_open_items_tmp_30ma",
        "pk": ["kunnr", "belnr", "docln", "ryear"],
        "filter": "kunnr IS NOT NULL AND kunnr <> '' AND netdt IS NOT NULL AND netdt <> '' "
                  "AND netdt <> '00000000' AND budat >= '20261101'", "max_rows": 30000,
        "slice": "本月记账且到期日有效；全量同口径约 11.9 万条，此处为示例规模",
        "attrs": ["kunnr", "belnr", "docln", "ryear", "name1", "blart", "drcrk",
                  "is_ab", "ksl", "netdt", "budat", "augbl"],
        "metric": {"cube": "ar_open_items", "key": "kunnr",
                   "label": "该客户应收", "member": "ar_open_items.customer_code",
                   "metrics": ["ar_balance", "ar_long_balance", "ar_b_181_360", "ar_b_721"]},
    },
    # ── 资金 ─────────────────────────────────────────────
    {
        "id": "FundBankBalance", "cn": "银行资金余额", "tier": 3,
        "conn": "doris-ads", "schema": "dwh_ads",
        "table": "ads_fund_bank_balance_daily_1da",
        "pk": ["data_date", "bank_name", "currency_code"],
        "filter": None, "max_rows": 10000,
        "attrs": ["data_date", "bank_name", "currency_code", "balance", "rate", "balance_cny"],
        "metric": {"cube": "fund_bank_balance", "key": "bank_name",
                   "label": "该银行资金余额", "member": "fund_bank_balance.bank_name",
                   "metrics": ["balance_cny", "balance"]},
    },
    {
        "id": "FundFinancingMaturity", "cn": "融资到期", "tier": 3,
        "conn": "doris-ads", "schema": "dwh_ads",
        "table": "ads_fund_financing_maturity_detail_1da",
        "pk": ["data_date", "contract_no"],
        "filter": "principal_balance <> 0 AND maturity_date IS NOT NULL", "max_rows": 5000,
        "attrs": ["data_date", "contract_no", "contract_text_no", "loan_term_type",
                  "principal_balance", "maturity_date"],
        "metric": {"cube": "fund_financing_maturity", "key": "contract_no",
                   "label": "本合同融资", "member": "fund_financing_maturity.contract_no",
                   "metrics": ["principal_balance"]},
    },
    # ── OTD ──────────────────────────────────────────────
    {
        "id": "OtdSchedule", "cn": "OTD排产明细", "tier": 3,
        "conn": "doris-ads", "schema": "dwh_ads",
        "table": "ads_otd_production_schedule_detail",
        "pk": ["factory_code", "material_no", "production_month"],
        "filter": None, "max_rows": 5000,
        "attrs": ["factory_code", "factory_name", "material_no", "production_month",
                  "vehicle_model", "plan_qty", "online_qty", "inbound_qty"],
        "metric": {"cube": "otd_schedule_detail", "key": "factory_code",
                   "label": "本工厂排产", "member": "otd_schedule_detail.factory_code",
                   "metrics": ["plan_qty", "online_qty", "inbound_qty"]},
    },
]

TYPE_MAP = {
    "varchar": "string", "char": "string", "text": "string", "string": "string",
    "int": "integer", "bigint": "integer", "smallint": "integer", "tinyint": "integer",
    "decimal": "decimal", "double": "decimal", "float": "decimal",
    "date": "datetime", "datetime": "datetime", "timestamp": "datetime",
}


def fetch_columns(schema: str, table: str) -> dict[str, str]:
    conn = pymysql.connect(database="dwh_dwd", connect_timeout=15, **DORIS)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return {r[0]: (r[1] or "").lower() for r in cur.fetchall()}
    finally:
        conn.close()


def main() -> int:
    db = SessionLocal()
    try:
        conns = {c.name: c for c in db.query(Connection).all()}
        missing = {s["conn"] for s in SPECS} - set(conns)
        if missing:
            print(f"缺少连接: {missing}")
            return 1

        for spec in SPECS:
            eid = spec["id"]
            conn = conns[spec["conn"]]
            columns = fetch_columns(spec["schema"], spec["table"])
            if not columns:
                print(f"  跳过 {eid}: 表不存在 {spec['table']}")
                continue

            # ── 资产 ──
            asset = db.query(Asset).filter(Asset.alias == f"{eid}_asset").first()
            if asset is None:
                asset = Asset(
                    name=spec["table"], alias=f"{eid}_asset", kind="table",
                    description=f"{spec['cn']}（Cube cube: {spec['metric']['cube'] if spec['metric'] else '-'}）",
                    connection_id=conn.id,
                    locator={"schema": spec["schema"], "table": spec["table"]},
                    primary_key=spec["pk"], domain="doris", tags=["doris", "real"],
                )
                db.add(asset)
                db.flush()

            # ── 对象类型 ──
            cfg = {"primary_key": spec["pk"]}
            if spec.get("filter"):
                cfg["filter_expr"] = spec["filter"]
            if spec.get("max_rows"):
                cfg["index_max_rows"] = spec["max_rows"]
            if spec.get("slice"):
                cfg["slice_label"] = spec["slice"]

            entity = db.get(OntologyEntity, eid)
            if entity is None:
                entity = OntologyEntity(
                    id=eid, name=eid, name_cn=spec["cn"], tier=spec["tier"],
                    description=f"{spec['cn']}（源表 {spec['table']}）",
                    config_json=cfg,
                )
                db.add(entity)
                db.flush()
            else:
                entity.config_json = cfg

            # ── 属性（只建真实存在的列） ──
            existing = {a.name for a in entity.attributes}
            added = 0
            for col in spec["attrs"]:
                if col in existing or col not in columns:
                    continue
                db.add(EntityAttribute(
                    entity_id=eid, name=col,
                    type=TYPE_MAP.get(columns[col], "string"),
                    description="", required=col in spec["pk"],
                    source_table=spec["table"], source_field=col,
                    data_status="confirmed",
                ))
                added += 1
            if added:
                db.flush()

            # ── 主绑定 ──
            if not db.query(ObjectBinding).filter(
                ObjectBinding.object_type_id == eid,
                ObjectBinding.role == "primary",
            ).first():
                db.add(ObjectBinding(
                    object_type_id=eid, asset_id=asset.id, role="primary",
                    id_column=spec["pk"][0],
                    field_mappings=[{"attribute": c, "column": c}
                                    for c in spec["attrs"] if c in columns],
                ))

            # ── 指标绑定 ──
            mb = spec.get("metric")
            if mb and not db.query(ObjectMetricBinding).filter(
                ObjectMetricBinding.object_type_id == eid,
                ObjectMetricBinding.cube_name == mb["cube"],
            ).first():
                db.add(ObjectMetricBinding(
                    object_type_id=eid, label=mb["label"],
                    cube_name=mb["cube"], key_column=mb["key"],
                    filter_member=mb["member"], metric_names=mb["metrics"],
                    description=f"按 {mb['key']} 过滤 Cube {mb['cube']}",
                ))

            flag = "指标绑定" if mb else "无指标绑定(维度不含主键)"
            print(f"  {eid:24} {spec['cn']:12} 属性{len(spec['attrs']):2}  "
                  f"上限{spec['max_rows']:>6}  {flag}")

        db.commit()
        print("\n完成。请注意：大表用了 filter_expr 收窄，见对象 config_json。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
