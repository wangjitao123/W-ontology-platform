"""自动发现的规则测试。

这些规则决定"什么算一个对象类型"，改错会直接产出一堆噪声或者漏掉真实体。
所以每个过滤条件都用真实踩过的例子钉住：

    · `customer` 家族**没有 customer_code**，只有 customer_name/desc/type
      → 不能静默丢掉，要作为"身份风险"提出来（正好撞上语义层的 identity_by_name 约束）
    · `approve_process_instance` / `file` / `pay` 这些长得像 *_id，但是技术对象
      → 必须进黑名单，否则会冒出 ApproveProcessInstance 这种候选
    · 165 张表刚开始推出 107 个实体、458 个关系
      → 太吵，必须收窄到能看的人数

不连数据库：用构造的 TableMeta 跑纯规则。
"""
from __future__ import annotations

import pytest

from app.services.discovery.profiler import (
    ColumnMeta,
    TableMeta,
    _attach_unsuffixed_columns,
    classify_column,
)
from app.services.discovery.rules import (
    build_proposals,
    discover_entities,
    discover_relations,
    discover_snapshots,
)


def tbl(schema: str, name: str, cols: list[tuple[str, str]]) -> TableMeta:
    t = TableMeta(schema=schema, name=name)
    for cname, dtype in cols:
        role, family = classify_column(cname)
        t.columns.append(ColumnMeta(name=cname, dtype=dtype, role=role, family=family))
    # 真实扫描里 scan() 会把"无后缀但首词属于已知家族"的列挂上去
    # （material_weight → material）。测试构造的数据要走同一步，
    # 否则测的规则和线上跑的不是一回事。
    _attach_unsuffixed_columns([t])
    return t


# ── 列名分类 ──────────────────────────────────────────────

@pytest.mark.parametrize(
    ("column", "role", "family"),
    [
        ("material_code", "key", "material"),
        ("material_name", "label", "material"),
        ("customer_type", "category", "customer"),
        ("approve_state_str", "status", "approve"),
        ("record_date", "datetime", "record"),
        ("total_stock_value", "measure", "total_stock"),
        ("etl_load_time", "ignore", None),
        ("id", "ignore", None),
        ("some_random_col", "other", None),
    ],
)
def test_classify_column(column, role, family):
    assert classify_column(column) == (role, family)


# ── 实体推导 ──────────────────────────────────────────────

def test_entity_from_key_and_label_family():
    tables = [
        tbl("dwh_dwd", "dwd_sap_inv_wms_aging_detail_1da",
            [("material_code", "varchar"), ("material_name", "varchar"),
             ("total_stock_value", "decimal")]),
        tbl("dwh_dwd", "dwd_other",
            [("material_code", "varchar"), ("material_name", "varchar")]),
    ]
    ents = discover_entities(tables)
    names = {e.name for e in ents}
    assert "Material" in names
    m = next(e for e in ents if e.name == "Material")
    assert m.key_field == "material_code"
    assert m.name_cn == "物料"
    assert m.identity_risk is False
    # 度量列不该进属性
    assert all(a["name"] != "total_stock_value" for a in m.attributes)


def test_entity_without_key_is_flagged_not_dropped():
    """customer 家族只有名称没有编码——要提出来并标注身份风险。

    静默丢掉它才是错的：人需要知道"你的客户主数据缺编码"，
    而按名称当身份正是语义层 identity_by_name 约束警告的事。
    """
    tables = [
        tbl("dwh_dwd", f"t{i}", [("customer_name", "varchar"), ("customer_type", "varchar")])
        for i in range(4)
    ]
    ents = discover_entities(tables)
    c = next((e for e in ents if e.name == "Customer"), None)
    assert c is not None, "没有编码列的客户族也必须作为候选出现"
    assert c.identity_risk is True
    assert c.key_field is None
    assert c.confidence == "low"
    assert "名称" in c.reason


def test_noise_family_is_excluded():
    """approve_process_instance 这类技术家族不能变成对象类型。"""
    tables = [
        tbl("dwh_dwd", f"t{i}",
            [("approve_process_instance_id", "varchar"),
             ("approve_process_instance_name", "varchar")])
        for i in range(5)
    ]
    ents = discover_entities(tables)
    assert not [e for e in ents if "Approve" in e.name]


def test_foreign_key_only_family_is_excluded():
    """只有 *_code 没有任何描述列 → 只是外键，不是对象。"""
    tables = [
        tbl("dwh_dwd", f"t{i}", [("chance_code", "varchar"), ("amount", "decimal")])
        for i in range(5)
    ]
    ents = discover_entities(tables)
    assert not [e for e in ents if e.name == "Chance"]


def test_entity_attribute_types():
    tables = [
        tbl("dwh_dwd", "a", [("material_code", "varchar"), ("material_name", "varchar")]),
        tbl("dwh_dwd", "b", [("material_code", "varchar"), ("material_weight", "decimal")]),
    ]
    ents = discover_entities(tables)
    m = next(e for e in ents if e.name == "Material")
    types = {a["name"]: a["type"] for a in m.attributes}
    assert types["material_code"] == "string"
    assert types["material_weight"] == "decimal"
    # 同一列在不同表里出现，只能算一个属性
    assert [a["name"] for a in m.attributes].count("material_code") == 1


# ── 快照推导 ──────────────────────────────────────────────

def test_snapshot_needs_date_and_measure():
    tables = [
        tbl("dwh_dws", "dws_sap_inv_aging_summary_1da",
            [("record_date", "date"), ("material_code", "varchar"),
             ("total_stock_value", "decimal"), ("total_stock_qty", "decimal")]),
    ]
    snaps = discover_snapshots(tables)
    assert len(snaps) == 1
    assert snaps[0].kind == "snapshot"
    assert snaps[0].extra["dateColumn"] == "record_date"
    assert snaps[0].extra["measureCount"] == 2


def test_table_without_date_is_not_snapshot():
    tables = [tbl("dwh_dws", "dws_x", [("material_code", "varchar"), ("qty_total", "decimal")])]
    assert discover_snapshots(tables) == []


def test_table_with_many_dimensions_is_not_snapshot():
    """维度太多说明是明细表，不是汇总。"""
    cols = [("record_date", "date")] + [(f"dim{i}_code", "varchar") for i in range(5)]
    cols += [("total_qty", "decimal")]
    tables = [tbl("dwh_dws", "dws_big", cols)]
    assert discover_snapshots(tables) == []


# ── 关系推导 ──────────────────────────────────────────────

def _two_entities_tables(n_cooccur: int):
    """构造 material / plant 两个实体，共现在 n 张表里。"""
    tables = [
        tbl("dwh_dwd", "master_material",
            [("material_code", "varchar"), ("material_name", "varchar")]),
        tbl("dwh_dwd", "master_plant",
            [("plant_code", "varchar"), ("plant_name", "varchar")]),
    ]
    for i in range(n_cooccur):
        tables.append(tbl(
            "dwh_dwd", f"fact_{i}",
            [("material_code", "varchar"), ("plant_code", "varchar")],
        ))
    return tables


def test_relation_needs_min_cooccurrence():
    """两张表共现是巧合，三张以上才提关系。"""
    ents = discover_entities(_two_entities_tables(3))
    few = discover_entities(_two_entities_tables(2))

    rels_ok = discover_relations(ents, _two_entities_tables(3))
    rels_few = discover_relations(few, _two_entities_tables(2))

    assert any(r.name == "Material-Plant" for r in rels_ok)
    assert not any(r.name == "Material-Plant" for r in rels_few)


def test_relation_endpoints_are_entity_names():
    ents = discover_entities(_two_entities_tables(3))
    rels = discover_relations(ents, _two_entities_tables(3))
    r = next(r for r in rels if r.name == "Material-Plant")
    assert r.extra["from"] == "ent::material"
    assert r.extra["to"] == "ent::plant"


# ── 汇总 ──────────────────────────────────────────────────

def test_build_proposals_shape():
    out = build_proposals(_two_entities_tables(3))
    assert set(out) >= {"entities", "snapshots", "relations", "stats"}
    for bucket in ("entities", "snapshots", "relations"):
        for p in out[bucket]:
            d = p.to_dict() if hasattr(p, "to_dict") else p
            assert d["id"] and d["kind"] and d["reason"]
            # 每条候选都必须能说清依据
            assert d["confidence"] in ("high", "medium", "low")


def test_entity_limit_caps_output():
    """候选上限：165 张表原本能推 107 个，必须收窄。"""
    tables = []
    for i in range(60):
        tables.append(tbl("dwh_dwd", f"t{i}",
                          [(f"fam{i}_code", "varchar"), (f"fam{i}_name", "varchar")]))
    ents = discover_entities(tables, limit=40)
    assert len(ents) <= 40
