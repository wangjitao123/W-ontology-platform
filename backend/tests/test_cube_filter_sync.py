"""口径同步的 SQL 解析测试。

这个解析器决定了"能不能把 cube 的 WHERE 抄成对象的 filter_expr"。
抄错会导致对象集合和指标口径不一致（本轮之前 42% 合同对象指标为 0），
所以**保守**是这里的第一要求：拿不准就跳过，绝不猜。
"""
from pathlib import Path

import pytest

from app.services.cube_filter_sync import extract_filter, load_cube_sqls


def test_simple_select_without_where():
    table, where, reason = extract_filter(
        "SELECT * FROM dwh_dwd.dwd_crm_vehicle_di"
    )
    assert table == "dwd_crm_vehicle_di"
    assert where is None
    assert reason is None


def test_simple_select_with_where():
    table, where, reason = extract_filter(
        "SELECT * FROM dwh_dwd.dwd_crm_contract_vehicle_di "
        "WHERE contract_deleted = 0 AND sign_state = 4"
    )
    assert table == "dwd_crm_contract_vehicle_di"
    assert where == "contract_deleted = 0 AND sign_state = 4"
    assert reason is None


def test_alias_prefix_is_stripped():
    """对象的 filter_expr 作用在未加别名的表上，别名前缀必须去掉。"""
    table, where, _ = extract_filter(
        "SELECT * FROM dwh_dwd.t AS t WHERE t.deleted = 0"
    )
    assert table == "t" or table == "t"        # FROM 的最后一段
    assert where == "deleted = 0"


@pytest.mark.parametrize("sql,keyword", [
    ("WITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
    ("SELECT * FROM a UNION SELECT * FROM b", "UNION"),
    ("SELECT * FROM a JOIN b ON a.id = b.id", "JOIN"),
    ("SELECT a, count(*) FROM t GROUP BY a", "GROUP BY"),
    ("SELECT * FROM t WHERE id IN (SELECT id FROM u)", "子查询"),
])
def test_unsafe_sql_is_skipped_with_reason(sql, keyword):
    table, where, reason = extract_filter(sql)
    assert table is None and where is None
    assert reason is not None
    assert keyword.lower() in reason.lower() or "子查询" in reason


def test_where_keeps_parentheses_and_in_lists():
    _, where, _ = extract_filter(
        "SELECT * FROM dwh_dwd.t WHERE racct IN ('1122','2203') AND deleted = 0"
    )
    assert "racct IN" in where
    assert where.endswith("deleted = 0")


def test_load_cube_sqls_missing_dir_returns_empty(tmp_path: Path):
    assert load_cube_sqls(tmp_path / "nope") == {}


def test_load_cube_sqls_reads_yaml(tmp_path: Path):
    (tmp_path / "sales.yml").write_text(
        "cubes:\n"
        "- name: contract_vehicle\n"
        "  sql: 'SELECT * FROM dwh_dwd.dwd_crm_contract_vehicle_di "
        "WHERE sign_state = 4'\n",
        encoding="utf-8",
    )
    sqls = load_cube_sqls(tmp_path)
    assert "contract_vehicle" in sqls
    table, where, _ = extract_filter(sqls["contract_vehicle"])
    assert table == "dwd_crm_contract_vehicle_di"
    assert where == "sign_state = 4"
