"""护栏引擎测试。

这层决定"要不要拒绝返回一个数字"，是本轮最需要钉死的地方。
用真实会算错数的查询形态做用例，包括本轮实测的那个：
对跨 143 天快照表做 SUM，膨胀 89 倍。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.services.guardrail_engine import check_cube_query, check_sql


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


# ── 快照跨天聚合（本轮实测的错数类型）──────────────────────

def test_blocks_aging_sum_without_date(db):
    """对库龄表 SUM 却不带 record_date —— 实测会膨胀 89 倍。"""
    sql = (
        "SELECT SUM(total_stock_value) AS v "
        "FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da"
    )
    r = check_sql(db, sql)
    assert r.blocking, "应当拦截"
    assert any("record_date" in v.reason for v in r.blocking)


def test_allows_aging_sum_with_date_filter(db):
    sql = (
        "SELECT SUM(total_stock_value) AS v "
        "FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da "
        "WHERE record_date = (SELECT MAX(record_date) FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da)"
    )
    assert not check_sql(db, sql).blocking


def test_allows_aging_sum_with_date_in_select(db):
    """按日期分组也算限定——这是跨期对比的正确写法。"""
    sql = (
        "SELECT record_date, SUM(total_stock_value) "
        "FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da "
        "GROUP BY record_date"
    )
    assert not check_sql(db, sql).blocking


def test_ignores_math_own_tables(db):
    """不涉及快照表的聚合不该被误拦。"""
    sql = "SELECT SUM(contract_total_price) FROM dwh_dwd.dwd_crm_abroad_contract_vehicle_di"
    assert not check_sql(db, sql).blocking


@pytest.mark.parametrize("cube", ["wms_detail", "aging_summary", "b2b_detail", "vehicle_detail"])
def test_aging_cubes_are_not_blocked_after_cube_fix(db, cube):
    """2026-11-08：库龄 cube 的 SQL 内已限定最新快照日，cube 路径不再需要拦截。

    这条测试是从 test_blocks_aging_cube_without_date 改过来的——
    cube 修好之后，再拦就是误拦。拦截能力保留给 ad_hoc 的裸 SQL（见上面几个用例）。
    """
    q = {"measures": [f"{cube}.some_measure"], "limit": 10}
    r = check_cube_query(db, q, cube)
    assert not r.blocking, f"{cube} 已内置日期过滤，不应再被拦截"


def test_aging_cube_sql_actually_has_date_filter():
    """直接读 cube 模型，确认 WHERE ... MAX(...) 真的写进去了。

    光靠"不拦截"证明不了 cube 安全——也可能只是检测器配错了。
    所以从 YAML 里核对实际 SQL。
    """
    import yaml
    from pathlib import Path

    from app.config import settings

    model_dir = Path(settings.CUBE_MODEL_DIR or "")
    if not model_dir.is_dir():
        pytest.skip("cube 模型目录不存在")

    aging_cubes = {"wms_detail", "b2b_detail", "vehicle_detail",
                   "aging_summary", "aging_top5"}
    found = {}
    for path in model_dir.glob("*.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for c in doc.get("cubes") or []:
            if c.get("name") in aging_cubes:
                found[c["name"]] = " ".join(str(c.get("sql") or "").split())

    assert set(found) == aging_cubes, f"模型里没找到全部库龄 cube: {set(found)}"
    for name, sql in found.items():
        assert "MAX(record_date)" in sql, f"{name} 的 SQL 缺少最新快照日过滤: {sql[:120]}"
        assert "record_date =" in sql, f"{name} 的 SQL 缺少 record_date 等值过滤"


def test_allows_aging_cube_with_date_filter(db):
    q = {
        "measures": ["aging_summary.aging_gt_180_value_total"],
        "filters": [{"member": "aging_summary.record_date", "operator": "equals",
                     "values": ["2026-11-07"]}],
    }
    assert not check_cube_query(db, q, "aging_summary").blocking


# ── 身份用名称 ────────────────────────────────────────────

def test_blocks_count_distinct_name(db):
    sql = (
        "SELECT COUNT(DISTINCT material_name) FROM "
        "dwh_dwd.dwd_sap_inv_wms_aging_detail_1da"
    )
    r = check_sql(db, sql)
    assert any(v.detector == "identity_by_name" and v.violated for v in r.verdicts)


def test_allows_count_distinct_code(db):
    sql = (
        "SELECT COUNT(DISTINCT material_code) FROM "
        "dwh_dwd.dwd_sap_inv_wms_aging_detail_1da"
    )
    r = check_sql(db, sql)
    assert not any(v.detector == "identity_by_name" and v.violated for v in r.verdicts)


# ── 缺口径过滤 ────────────────────────────────────────────

def test_blocks_contract_query_without_scope(db):
    sql = "SELECT COUNT(*) FROM dwh_dwd.dwd_crm_contract_vehicle_di"
    r = check_sql(db, sql)
    assert any(v.detector == "required_scope_filter" and v.violated for v in r.verdicts)


def test_allows_contract_query_with_scope(db):
    sql = (
        "SELECT COUNT(*) FROM dwh_dwd.dwd_crm_contract_vehicle_di "
        "WHERE contract_deleted = 0 AND sign_state = 4"
    )
    r = check_sql(db, sql)
    assert not any(v.detector == "required_scope_filter" and v.violated for v in r.verdicts)


# ── 聚合级下钻 ────────────────────────────────────────────

def test_blocks_finance_drilldown(db):
    q = {
        "measures": ["finance_summary.total_assets"],
        "dimensions": ["finance_summary.customer_name"],
    }
    r = check_cube_query(db, q, "finance_summary")
    assert r.blocking
    assert any(v.detector == "forbidden_drilldown" for v in r.blocking)


def test_allows_finance_by_period(db):
    q = {
        "measures": ["finance_summary.total_assets"],
        "filters": [{"member": "finance_summary.period", "operator": "equals",
                     "values": ["2026-07"]}],
    }
    assert not check_cube_query(db, q, "finance_summary").blocking


# ── 跨命名空间 JOIN ───────────────────────────────────────

def test_blocks_cross_namespace_join(db):
    sql = (
        "SELECT COUNT(*) FROM dwh_dwd.dwd_crm_contract_vehicle_di a "
        "JOIN dwh_dwd.dwd_crm_abroad_contract_vehicle_di b ON a.contract_code = b.contract_code"
    )
    r = check_sql(db, sql)
    assert any(v.detector == "cross_namespace_join" and v.violated for v in r.verdicts)


# ── 拦截语义 ──────────────────────────────────────────────

def test_only_block_severity_enforces(db):
    """warn 级约束即使命中也不该拦截。"""
    sql = (
        "SELECT SUM(balance_cny) FROM dwh_ads.ads_fund_bank_balance_daily_1da"
    )
    r = check_sql(db, sql)
    # fund_bank_daily 是 warn 级：会出现在 verdicts 里，但不 blocking
    assert any(v.detector == "snapshot_aggregate_without_date" for v in r.verdicts)
    assert not r.blocking


def test_blocked_result_is_bypassable(db):
    """拦截结果必须带 bypassable 标记，否则用户永远看不到原始数。"""
    sql = "SELECT SUM(total_stock_value) FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da"
    r = check_sql(db, sql)
    assert r.blocking
    d = r.to_dict()
    assert d["blocked"] is True
    assert d["blocking"][0]["enforced"] is True
    assert d["blocking"][0]["remedy"], "拦截必须给出修正方式"

def test_count_distinct_is_not_blocked(db):
    """COUNT(DISTINCT 编码) 跨天不会重复计数，不该拦。"""
    sql = (
        "SELECT COUNT(DISTINCT material_code) FROM "
        "dwh_dwd.dwd_sap_inv_wms_aging_detail_1da"
    )
    r = check_sql(db, sql)
    assert not r.blocking, "COUNT(DISTINCT) 不应被拦截（这是误拦）"


def test_plain_count_is_low_confidence(db):
    """COUNT(*) 跨天语义不对，但只提示不拦。"""
    sql = "SELECT COUNT(*) FROM dwh_dwd.dwd_sap_inv_wms_aging_detail_1da"
    r = check_sql(db, sql)
    hits = [v for v in r.verdicts if v.detector == "snapshot_aggregate_without_date"]
    assert hits and hits[0].confidence == "low"
    assert not r.blocking

# ── 存量度量跨期求和（OTD 排产）──────────────────────────────

def test_blocks_stock_measure_summed_without_period(db):
    """期初库存是存量，不限生产月求和会叠加多个期（实测 4.2 倍）。"""
    q = {"measures": ["otd_schedule_detail.opening_inventory"], "limit": 10}
    r = check_cube_query(db, q, "otd_schedule_detail")
    assert r.blocking, "存量度量跨期求和应当拦截"
    assert any(v.detector == "stock_measure_summed" for v in r.blocking)


def test_allows_stock_measure_with_production_month(db):
    q = {
        "measures": ["otd_schedule_detail.opening_inventory"],
        "filters": [{"member": "otd_schedule_detail.production_month",
                     "operator": "equals", "values": ["202611"]}],
    }
    assert not check_cube_query(db, q, "otd_schedule_detail").blocking


def test_allows_flow_measure_without_period(db):
    """plan_qty 是流量，跨月求和是对的，不能拦。"""
    q = {"measures": ["otd_schedule_detail.plan_qty"], "limit": 10}
    assert not check_cube_query(db, q, "otd_schedule_detail").blocking


def test_otd_daily_cubes_are_not_blocked(db):
    """otd_daily / distribution_daily 是事件口径，跨日累加正确，不该拦。"""
    for cube in ("otd_daily", "otd_distribution_daily"):
        q = {"measures": [f"{cube}.online_qty"], "limit": 10}
        assert not check_cube_query(db, q, cube).blocking, f"{cube} 是事件口径，不应拦截"
