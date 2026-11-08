"""语义层内容：证据 + 约束 + 别名/粒度回填。

## 这里填的都不是编的

每一条约束都来自本轮真实踩过的坑，每条证据都有真实执行过的查询和结果：

| 约束 | 来源 |
| --- | --- |
| 库龄快照跨天不能 SUM | 建对象时发现"同一物料每天是新对象"，被迫加 slice_expr |
| 物料一名多码不能按名字合并 | 体检发现 8,924 码 / 3,881 名，1,040 组一对多 |
| 合同口径必须带 sign_state=4 | 42% 合同对象指标查出 0 的那个 bug |
| 国内外合同编码空间不同 | 试图建 ContractVehicle↔AbroadContract 关联，匹配数 = 0 |
| composition 关系两端不能同时求和 | 物料↔库龄 12,958 条边，两端求和即重复计算 |
| 财务指标是公司级聚合 | finance_summary 每期一行，共 19 行 |
| 应收未清项只取本月切片 | 源表 88,110 行 → 索引 607 行 |
| 银行余额是日快照 | 8,075 行按 (date, bank, currency) |

## 幂等

按固定 id upsert；重跑不会产生重复。

## 用法

    python -m scripts.seed_semantic_layer
"""
from __future__ import annotations

import io
import sys

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.database import SessionLocal  # noqa: E402
from app.models.entity import EntityAttribute, OntologyEntity  # noqa: E402
from app.models.relation import EntityRelation  # noqa: E402
from app.models.semantic import SemanticConstraint, SemanticEvidence  # noqa: E402

# ── 证据 ──────────────────────────────────────────────────
# observed_at 用固定日期，保证重跑结果稳定
EVIDENCE = [
    dict(
        id="ev_wms_snapshot_date",
        title="零部件库龄只有单一快照日",
        source="doris_query",
        query="SELECT record_date, COUNT(*) FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da "
              "GROUP BY record_date ORDER BY record_date DESC LIMIT 5",
        result="索引后 12,958 行，record_date 只有 2026-11-07 一天（源表 179 万行跨多个日期）",
        validation_method="distinct",
    ),
    dict(
        id="ev_material_code_name_ratio",
        title="物料一名多码（SAP 同描述不同料号）",
        source="doris_query",
        query="SELECT COUNT(DISTINCT material_code), COUNT(DISTINCT material_name) "
              "FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da",
        result="物料码 8,615 个 / 名称 3,881 个；1,040 个名称对应多个码（三表并集共 8,924 个物料）",
        validation_method="distinct",
    ),
    dict(
        id="ev_material_cross_table_names",
        title="物料名称跨表一致（无冲突）",
        source="doris_query",
        query="按 material_code JOIN 零部件与 B2件两表，统计同一码名称不同的数量",
        result="冲突 0 个 —— 所以可以安全地从事实表反推物料主数据",
        validation_method="cross_check",
    ),
    dict(
        id="ev_contract_scope_bug",
        title="合同对象口径不一致导致 42% 指标为 0",
        source="data_profile",
        query="对比对象索引行数与 cube 口径行数（contract_deleted=0 AND sign_state=4）",
        result="对象未收窄时大量合同在 Cube 口径下查不到，指标显示 0；"
               "收窄到 3,158 行后 20/20 抽样全部有值",
        validation_method="cross_check",
    ),
    dict(
        id="ev_contract_code_namespace",
        title="国内外合同编码空间不相交",
        source="doris_query",
        query="SELECT COUNT(*) FROM (SELECT DISTINCT contract_code FROM ContractVehicle) a "
              "JOIN (SELECT DISTINCT contract_code FROM AbroadContract) b USING (contract_code)",
        result="交集 0 —— 国内为 DW 前缀，海外为 DWHV 前缀，不能按 contract_code 关联",
        validation_method="cross_check",
    ),
    dict(
        id="ev_material_wms_link_edges",
        title="物料↔库龄是 1:N 组成关系",
        source="object_link",
        query="SELECT count(*) FROM object_link WHERE rel_id='rel_material_wms_aging'",
        result="12,958 条边：每个库龄快照行都归属于某个物料",
        validation_method="count",
    ),
    dict(
        id="ev_finance_month_grain",
        title="财务指标是公司级月度聚合",
        source="doris_query",
        query="SELECT COUNT(*), MIN(period), MAX(period) FROM dwh_dws.dws_cdep_financial_indicator_month",
        result="19 行，period 从 2025-01 到 2026-07，每期一行全集团合并口径",
        validation_method="count",
    ),
    dict(
        id="ev_ar_open_items_slice",
        title="应收未清项源表远大于索引切片",
        source="doris_query",
        query="SELECT COUNT(*) FROM dwh_dwd.dwd_sap_finance_open_items_30ma",
        result="源表 88,110 行（30 天滚动），对象索引只取本月切片 607 行",
        validation_method="count",
    ),
    dict(
        id="ev_fund_bank_daily",
        title="银行资金余额是日快照",
        source="doris_query",
        query="SELECT COUNT(*), COUNT(DISTINCT data_date) FROM dwh_ads.ads_fund_bank_balance_daily_1da",
        result="8,075 行，按 (data_date, bank_name, currency_code) 每条日快照一行",
        validation_method="distinct",
    ),
    dict(
        id="ev_customer_uniqueness",
        title="客户码近乎唯一，但存在一名多码",
        source="doris_query",
        query="SELECT COUNT(DISTINCT kunnr), COUNT(DISTINCT name1) FROM "
              "dwh_dwd.dwd_sap_finance_open_items_tmp_30ma",
        result="客户码 2,576 个 / 名称 2,501 个；39 个名称对应多个码",
        validation_method="distinct",
    ),
]

# ── 约束 ──────────────────────────────────────────────────
CONSTRAINTS = [
    dict(
        id="sem_c_aging_snapshot_grain",
        name="库龄快照跨天不能直接求和",
        target_kind="entity", target_id="WmsAging",
        constraint_type="grain", severity="block", priority=10,
        description="零部件库龄按 (record_date, material_code, plant_code, warehouse_code) "
                    "存快照，当前索引只含 2026-11-07 一天。把多个日期的 "
                    "total_stock_qty / total_stock_value 直接 SUM 会把同一天的库存重复累加。",
        remedy="跨期对比先按 record_date 聚合到日再比较；不要对多天快照做一次性 SUM。",
        trigger_keywords=["库存", "库龄", "总量", "总价值", "求和", "sum", "跨月", "趋势"],
        evidence_ids=["ev_wms_snapshot_date"],
    ),
    dict(
        id="sem_c_material_identity",
        name="物料身份必须用物料码，不能用名称",
        target_kind="entity", target_id="Material",
        constraint_type="identity", severity="block", priority=10,
        description="8,924 个物料码只对应 3,881 个名称，1,040 个名称对应多个码。"
                    "这是 SAP 里同描述不同料号的正常现象，不是脏数据。按名称合并会算错物料数。",
        remedy="身份一律用 material_code；名称仅用于展示。按名称去重会低估物料数量。",
        trigger_keywords=["物料", "多少个物料", "物料数", "去重", "按名称"],
        evidence_ids=["ev_material_code_name_ratio", "ev_material_cross_table_names"],
    ),
    dict(
        id="sem_c_contract_scope",
        name="合同车辆口径必须带删除/审核条件",
        target_kind="entity", target_id="ContractVehicle",
        constraint_type="scope", severity="block", priority=10,
        description="合同车辆明细必须过滤 contract_deleted = 0 且 sign_state = 4（审核通过）。"
                    "不过滤会把已删除和未审核的合同算进来。Cube 的 contract_vehicle 已内置该口径。",
        remedy="任何合同数量/金额口径都要带这两个条件；对象层已同步为 filter_expr。",
        trigger_keywords=["合同", "合同数", "合同金额", "多少份合同"],
        evidence_ids=["ev_contract_scope_bug"],
    ),
    dict(
        id="sem_c_contract_code_namespace",
        name="国内外合同编码空间不相交",
        target_kind="relation", target_id="rel_abroad_contract_deliveries",
        constraint_type="identity", severity="block", priority=20,
        description="国内合同 contract_code 形如 DW20240914004，海外合同形如 DWHV20260033AE，"
                    "按 contract_code 求交集为 0。两者不能按编码关联或合并统计。",
        remedy="国内外分开统计；海外合同用 contract_id，国内用 contract_code + product_portfolio_id。",
        trigger_keywords=["海外合同", "合同", "国内外", "合并统计"],
        evidence_ids=["ev_contract_code_namespace"],
    ),
    dict(
        id="sem_c_composition_no_double_sum",
        name="组成关系的两端不能同时求和",
        target_kind="relation", target_id="rel_material_wms_aging",
        constraint_type="aggregation", severity="block", priority=20,
        description="物料↔库龄是 composition（库龄记录依附于物料）。"
                    "对子项求和得到「这个物料的库存」是有意义的；"
                    "但把物料数与库龄行数的数量相加就是重复计算。",
        remedy="沿关系聚合时只对一端求和，不要两端相加。",
        trigger_keywords=["库存", "合计", "总量", "加总"],
        evidence_ids=["ev_material_wms_link_edges"],
    ),
    dict(
        id="sem_c_finance_company_level",
        name="财务指标是公司级聚合，不能按对象下钻",
        target_kind="cube", target_id="finance_summary",
        constraint_type="aggregation", severity="block", priority=10,
        description="finance_summary 每个报表期一行，是全集团合并口径。"
                    "不存在「某客户的资产总额」这类切片，按客户/物料/合同过滤没有意义。",
        remedy="财务指标只能按 period 切片；要看分客户/分物料的数据请用对应的明细 cube。",
        trigger_keywords=["资产总额", "资产负债率", "营业收入", "净利润", "财务指标"],
        evidence_ids=["ev_finance_month_grain"],
    ),
    dict(
        id="sem_c_ar_open_item_slice",
        name="应收未清项只取本月切片",
        target_kind="entity", target_id="ArOpenItem",
        constraint_type="grain", severity="warn", priority=30,
        description="应收账款未清项源表 88,110 行（30 天滚动），对象索引只取本月切片 607 行。"
                    "跨月累加会把同一笔应收重复计算。",
        remedy="看「当前应收余额」用最新切片；看趋势要按快照日期分别取数再比较。",
        trigger_keywords=["应收", "未清", "应收账款", "账龄", "余额"],
        evidence_ids=["ev_ar_open_items_slice"],
    ),
    dict(
        id="sem_c_fund_bank_daily",
        name="银行资金余额是日快照",
        target_kind="entity", target_id="FundBankBalance",
        constraint_type="grain", severity="warn", priority=30,
        description="银行资金余额按 (data_date, bank_name, currency_code) 每天一行。"
                    "跨日期 SUM 会把不同日期的余额加在一起，得到无意义的数。",
        remedy="看「当前总余额」取最新 data_date；看趋势按 data_date 分组。",
        trigger_keywords=["资金", "余额", "银行", "资金余额"],
        evidence_ids=["ev_fund_bank_daily"],
    ),
    dict(
        id="sem_c_customer_name_not_identity",
        name="客户名称不能当身份",
        target_kind="entity", target_id="Customer",
        constraint_type="identity", severity="warn", priority=40,
        description="2,576 个客户码对应 2,501 个名称，有 39 个名称对应多个码。"
                    "按名称统计客户数会偏低。",
        remedy="客户身份用 customer_code（即 kunnr）；名称用于展示和模糊搜索。",
        trigger_keywords=["客户", "多少个客户", "客户数"],
        evidence_ids=["ev_customer_uniqueness"],
    ),
]

# ── 实体别名 + 置信度回填 ──────────────────────────────────
ENTITY_ALIASES: dict[str, list[str]] = {
    "ContractVehicle": ["合同车辆", "合同行", "合同明细", "contract line"],
    "AbroadContract": ["海外合同", "外销合同", "DWHV 合同"],
    "AbroadDelivery": ["海外交货单", "外销交货单", "出口发货单"],
    "ArOpenItem": ["应收未清项", "未清账款", "应收明细", "AR open item"],
    "Customer": ["客户", "客户主数据", "往来单位", "customer"],
    "Material": ["物料", "物料主数据", "料号", "material"],
    "Supplier": ["供应商", "厂商", "supplier", "vendor"],
    "Plant": ["工厂", "生产工厂", "plant"],
    "WmsAging": ["零部件库龄", "零部件库存库龄"],
    "B2bAging": ["B2件库龄", "B2B 库存库龄"],
    "VehicleAging": ["整车库龄", "整车库存"],
    "FundBankBalance": ["银行资金余额", "资金头寸", "银行存款"],
    "FundFinancingMaturity": ["融资到期", "借款到期", "融资明细"],
    "OtdSchedule": ["OTD排产", "排产明细", "生产排程"],
}

# ── 关系语义类型 ──────────────────────────────────────────
RELATION_SEMANTIC_TYPE = {
    "rel_abroad_contract_deliveries": "composition",
    "rel_material_wms_aging": "composition",
    "rel_material_b2b_aging": "composition",
    "rel_material_vehicle_aging": "composition",
    "rel_material_otd_schedule": "association",
    "rel_customer_ar_open_item": "composition",
    "rel_plant_wms_aging": "association",
}

# ── 属性粒度/聚合语义（只写确实知道的）────────────────────
# 数值列的聚合语义：快照表要先按日期去重
SUM_OK = "可 sum；但快照表需先按快照日期取单日再聚合，跨日直接加会重复计算"
NULLABLE_SUM = "可 sum，空值按 0 处理"
IDENTITY = "身份列，不参与聚合"
DESCRIBE = "描述列，不参与聚合"


def _attr_semantics(entity_id: str, attr_name: str) -> tuple[str | None, str | None]:
    """返回 (grain, aggregation_semantics)。只对确定的情况给值。"""
    snapshot_entities = {"WmsAging", "B2bAging", "VehicleAging", "FundBankBalance"}
    amount_cols = {
        "total_stock_qty", "total_stock_value", "plan_qty", "online_qty",
        "inbound_qty", "quantity", "balance", "balance_cny", "actual_amount",
        "contract_total_price", "ksl", "principal_balance", "total_assets",
        "total_liabilities", "revenue_month", "net_profit_month", "cash",
        "inventory", "accounts_receivable",
    }
    if attr_name in ("material_code", "customer_code", "supplier_code", "plant_code",
                     "contract_code", "contract_id", "delivery_id", "kunnr", "period"):
        return None, IDENTITY
    if attr_name in amount_cols:
        agg = SUM_OK if entity_id in snapshot_entities else NULLABLE_SUM
        grain = "按快照日期" if entity_id in snapshot_entities else None
        return grain, agg
    if attr_name.endswith("_name") or attr_name.endswith("_str") or attr_name in (
        "contract_title", "vehicle_model", "module_category", "type_str",
    ):
        return None, DESCRIBE
    return None, None


def main() -> int:
    db = SessionLocal()
    try:
        # ── 1) 证据 ──
        print("=== 证据 ===")
        ev_created = ev_updated = 0
        for spec in EVIDENCE:
            row = db.get(SemanticEvidence, spec["id"])
            if row is None:
                db.add(SemanticEvidence(**spec))
                ev_created += 1
            else:
                for k, v in spec.items():
                    if k != "id":
                        setattr(row, k, v)
                ev_updated += 1
        db.commit()
        print(f"  新建 {ev_created}，更新 {ev_updated}，共 {len(EVIDENCE)} 条")

        # ── 2) 约束（校验证据引用存在）──
        print("=== 约束 ===")
        ev_ids = {e["id"] for e in EVIDENCE}
        c_created = c_updated = 0
        for spec in CONSTRAINTS:
            missing = [x for x in spec["evidence_ids"] if x not in ev_ids]
            if missing:
                print(f"  ⚠️ {spec['id']} 引用了不存在的证据 {missing}")
            row = db.get(SemanticConstraint, spec["id"])
            if row is None:
                db.add(SemanticConstraint(**spec))
                c_created += 1
            else:
                for k, v in spec.items():
                    if k != "id":
                        setattr(row, k, v)
                c_updated += 1
        db.commit()
        print(f"  新建 {c_created}，更新 {c_updated}，共 {len(CONSTRAINTS)} 条")

        # ── 3) 实体别名 / 置信度 ──
        print("=== 实体别名 ===")
        ent_updated = 0
        for eid, aliases in ENTITY_ALIASES.items():
            e = db.get(OntologyEntity, eid)
            if e is None:
                print(f"  ⚠️ 实体不存在: {eid}")
                continue
            e.aliases = aliases
            # 对象类型都由真实数据反推并做过口径对齐 → high
            e.confidence = "high"
            ent_updated += 1
        db.commit()
        print(f"  更新 {ent_updated} 个实体")

        # ── 4) 关系语义类型 ──
        print("=== 关系语义类型 ===")
        rel_updated = 0
        for rid, stype in RELATION_SEMANTIC_TYPE.items():
            r = db.get(EntityRelation, rid)
            if r is None:
                continue
            r.semantic_type = stype
            r.confidence = "high"
            rel_updated += 1
        db.commit()
        print(f"  更新 {rel_updated} 条关系")

        # ── 5) 属性粒度/聚合语义 ──
        print("=== 属性粒度/聚合语义 ===")
        attr_updated = 0
        for a in db.query(EntityAttribute).all():
            grain, agg = _attr_semantics(a.entity_id, a.name)
            changed = False
            if grain and not a.grain:
                a.grain = grain
                changed = True
            if agg and not a.aggregation_semantics:
                a.aggregation_semantics = agg
                changed = True
            if a.source_field and a.confidence != "high":
                # 有物理列映射 = 可核对，给 high
                a.confidence = "high"
                changed = True
            if changed:
                attr_updated += 1
        db.commit()
        print(f"  更新 {attr_updated} 个属性")

        # ── 6) 汇总 ──
        print("=== 汇总 ===")
        print(f"  证据   : {db.query(SemanticEvidence).count()} 条")
        print(f"  约束   : {db.query(SemanticConstraint).count()} 条"
              f"（block {db.query(SemanticConstraint).filter(SemanticConstraint.severity == 'block').count()}）")
        print(f"  有别名 : {db.query(OntologyEntity).filter(OntologyEntity.aliases.isnot(None)).count()} 个实体")
        print(f"  有粒度 : {db.query(EntityAttribute).filter(EntityAttribute.grain.isnot(None)).count()} 个属性")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
