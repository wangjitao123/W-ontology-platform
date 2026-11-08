"""问数路由的关键闸门测试。

方案 B 的核心保证是：**LLM 只能在已定义的指标里挑，编不出新口径。**
这个保证由 `_validate_query` 实现——它拿 LLM 返回的查询去和 Cube 的目录比对，
任何不存在的成员都会被拒。所以这层必须钉死。
"""
from app.services.ask_service import _short_title, _validate_query

CATALOG = {
    "finance_summary": {
        "name": "finance_summary",
        "title": "财务指标月度汇总",
        "measures": [
            {"name": "finance_summary.total_assets", "title": "财务指标月度汇总 资产总额"},
            {"name": "finance_summary.debt_ratio", "title": "财务指标月度汇总 资产负债率"},
        ],
        "dimensions": [
            {"name": "finance_summary.period", "title": "财务指标月度汇总 报表期间"},
            {"name": "finance_summary.periodDate", "title": "财务指标月度汇总 报表期间(日期型)"},
        ],
    },
    "contract_vehicle": {
        "name": "contract_vehicle",
        "title": "合同车辆明细",
        "measures": [{"name": "contract_vehicle.contract_count", "title": "合同车辆明细 合同份数"}],
        "dimensions": [{"name": "contract_vehicle.province", "title": "合同车辆明细 省份"}],
    },
}


def test_valid_query_passes_through():
    q, err = _validate_query(
        {"measures": ["finance_summary.total_assets"],
         "filters": [{"member": "finance_summary.period", "operator": "equals",
                      "values": ["2026-07"]}],
         "limit": 5},
        CATALOG,
    )
    assert err is None
    assert q["measures"] == ["finance_summary.total_assets"]
    assert q["filters"][0]["values"] == ["2026-07"]
    assert q["limit"] == 5


def test_hallucinated_measure_is_rejected():
    """模型编一个不存在的指标 —— 必须拒绝，不能拿去查。"""
    q, err = _validate_query(
        {"measures": ["finance_summary.profit_margin_magic"]}, CATALOG,
    )
    assert q is None
    assert "指标不存在" in err


def test_hallucinated_dimension_is_rejected():
    q, err = _validate_query(
        {"measures": ["finance_summary.total_assets"],
         "dimensions": ["finance_summary.branch_office"]},
        CATALOG,
    )
    assert q is None
    assert "维度不存在" in err


def test_empty_measures_is_rejected():
    q, err = _validate_query({"measures": []}, CATALOG)
    assert q is None
    assert "没有选出任何指标" in err


def test_invalid_filter_member_is_dropped_not_fatal():
    """过滤器成员不存在时丢掉那一条，剩下的照常查（比整条拒绝更有用）。"""
    q, err = _validate_query(
        {"measures": ["finance_summary.total_assets"],
         "filters": [
             {"member": "finance_summary.period", "operator": "equals", "values": ["2026-07"]},
             {"member": "finance_summary.not_a_dim", "operator": "equals", "values": ["x"]},
         ]},
        CATALOG,
    )
    assert err is None
    assert len(q["filters"]) == 1
    assert q["filters"][0]["member"] == "finance_summary.period"


def test_time_dimension_is_kept_with_granularity():
    q, err = _validate_query(
        {"measures": ["finance_summary.total_assets"],
         "timeDimensions": [{"dimension": "finance_summary.periodDate",
                             "granularity": "month"}]},
        CATALOG,
    )
    assert err is None
    assert q["timeDimensions"][0]["granularity"] == "month"


def test_limit_is_clamped():
    q, _ = _validate_query(
        {"measures": ["finance_summary.total_assets"], "limit": 99999}, CATALOG,
    )
    assert q["limit"] <= 500


def test_short_title_strips_cube_prefix():
    assert _short_title("财务指标月度汇总 资产总额", "财务指标月度汇总") == "资产总额"
    assert _short_title("资产总额", "财务指标月度汇总") == "资产总额"
    assert _short_title(None, "x") == "(未命名)"
