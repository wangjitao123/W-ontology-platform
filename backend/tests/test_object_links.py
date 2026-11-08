"""实例级连线（物化）的测试。

测的是"把 join_keys 变成真实的边"这条链上最容易出错的地方：
    1. 等值连接算得对（含复合键）
    2. 变换（month 等）真的生效
    3. 源数据变了之后，旧边会被清掉
    4. 双向查询都拿得到（存储只存一个方向）
    5. 非法的列名不能拼进 SQL

用 ``__test_`` 前缀隔离，跑完清理；连不上 Postgres 时整组跳过。
"""
from __future__ import annotations

import uuid

import pytest

from app.services.object_store import _prop_expr, get_object_store, reset_object_store


@pytest.fixture(scope="module")
def store():
    from app.config import settings

    if not (settings.OBJECT_STORE_URL or "").strip():
        pytest.skip("未配置 OBJECT_STORE_URL")
    reset_object_store()
    s = get_object_store()
    if s is None:
        pytest.skip("对象存储不可用")
    yield s


@pytest.fixture()
def ids(store):
    """一对隔离的类型名 + 一个隔离的关系名。

    **清理放在 teardown 里，不靠测试末尾手动调 `_cleanup`。**

    原来的写法是每个用例末尾调 `_cleanup(store, ids)`：一旦中间的断言失败，
    那行就执行不到，孤儿对象和连线留在 Postgres 里。虽然 id 带随机后缀
    不会直接撞车，但残留会让后续对 `object_link` 的全局统计（比如
    "重建后应剩 0 条"）看到不该有的数据，表现为**换一次运行顺序就挂**。
    实测全量跑时偶发 `test_stale_links_are_removed` 失败、单跑必过。
    """
    base = uuid.uuid4().hex[:8]
    out = {
        "from": f"__test_from_{base}",
        "to": f"__test_to_{base}",
        "rel": f"__test_rel_{base}",
    }
    # 测试本体一开始是干净的（万一上一轮崩了留下同名残留）
    _cleanup(store, out)
    try:
        yield out
    finally:
        # 无论断言成功与否都清理，避免孤儿数据影响后续用例
        _cleanup(store, out)


def _seed(store, type_id: str, rows: list[tuple[str, dict]]) -> None:
    store.replace_type(
        type_id,
        [{"__object_key__": k, "__title__": k, **p} for k, p in rows],
        "test_tbl",
    )


def _cleanup(store, ids) -> None:
    store.delete_links(ids["rel"])
    store.delete_type(ids["from"])
    store.delete_type(ids["to"])


# ── 等值连接 ──────────────────────────────────────────────

def test_simple_equality_join(store, ids):
    _seed(store, ids["from"], [("a|1", {"contract_id": "C1"}),
                               ("a|2", {"contract_id": "C2"}),
                               ("a|3", {"contract_id": "C9"})])
    _seed(store, ids["to"], [("d1", {"contract_id": "C1"}),
                             ("d2", {"contract_id": "C1"}),
                             ("d3", {"contract_id": "C2"})])

    n = store.rebuild_links(ids["rel"], ids["from"], ids["to"],
                            [{"from_column": "contract_id", "to_column": "contract_id"}])
    # C1: 1×2 = 2 条；C2: 1×1 = 1 条；C9 无匹配
    assert n == 3
    _cleanup(store, ids)


def test_composite_join_keys(store, ids):
    """两个键必须同时相等才算一条边。"""
    _seed(store, ids["from"], [
        ("a|1", {"material_code": "M1", "plant_code": "P1"}),
        ("a|2", {"material_code": "M1", "plant_code": "P2"}),
    ])
    _seed(store, ids["to"], [
        ("b|1", {"material_code": "M1", "plant_code": "P1"}),
        ("b|2", {"material_code": "M1", "plant_code": "P3"}),
    ])
    n = store.rebuild_links(ids["rel"], ids["from"], ids["to"], [
        {"from_column": "material_code", "to_column": "material_code"},
        {"from_column": "plant_code", "to_column": "plant_code"},
    ])
    assert n == 1        # 只有 M1+P1 对上
    _cleanup(store, ids)


def test_transform_month(store, ids):
    """日期截成月份才能对上"财务月"的 period。"""
    _seed(store, ids["from"], [
        ("a|1", {"data_date": "2026-07-15"}),
        ("a|2", {"data_date": "2026-07-28"}),
        ("a|3", {"data_date": "2024-01-01"}),   # 不在目标月份范围内
    ])
    _seed(store, ids["to"], [("2026-07", {"period": "2026-07"})])

    n = store.rebuild_links(ids["rel"], ids["from"], ids["to"], [{
        "from_column": "data_date", "to_column": "period", "from_transform": "month",
    }])
    assert n == 2        # 同一月的两行都归属到这个月
    _cleanup(store, ids)


def test_null_and_empty_keys_do_not_join(store, ids):
    """空值不应该和空值连起来——两条都没有值不代表它们有关系。"""
    _seed(store, ids["from"], [("a|1", {"k": ""}), ("a|2", {})])
    _seed(store, ids["to"], [("b|1", {"k": ""}), ("b|2", {})])
    n = store.rebuild_links(ids["rel"], ids["from"], ids["to"],
                            [{"from_column": "k", "to_column": "k"}])
    # props->>'k' 对缺失键返回 NULL，NULL = NULL 在 SQL 里是 NULL（不成立）
    # 对空串则 "" = "" 成立，所以只会有空串那一对
    assert n == 1
    _cleanup(store, ids)


# ── 增量维护 ──────────────────────────────────────────────

def test_stale_links_are_removed(store, ids):
    _seed(store, ids["from"], [("a|1", {"k": "X"})])
    _seed(store, ids["to"], [("b|1", {"k": "X"})])
    assert store.rebuild_links(ids["rel"], ids["from"], ids["to"],
                               [{"from_column": "k", "to_column": "k"}]) == 1

    # 目标侧的数据没了，重算后边应该消失
    store.replace_type(ids["to"], [], "test_tbl")
    assert store.rebuild_links(ids["rel"], ids["from"], ids["to"],
                               [{"from_column": "k", "to_column": "k"}]) == 0
    _cleanup(store, ids)


def test_rebuild_is_idempotent(store, ids):
    _seed(store, ids["from"], [("a|1", {"k": "X"})])
    _seed(store, ids["to"], [("b|1", {"k": "X"}), ("b|2", {"k": "X"})])
    pairs = [{"from_column": "k", "to_column": "k"}]
    first = store.rebuild_links(ids["rel"], ids["from"], ids["to"], pairs)
    second = store.rebuild_links(ids["rel"], ids["from"], ids["to"], pairs)
    assert first == second == 2      # 不会因为重复执行翻倍
    _cleanup(store, ids)


# ── 双向查询 ──────────────────────────────────────────────

def test_links_of_object_both_directions(store, ids):
    _seed(store, ids["from"], [("a|1", {"k": "X"})])
    _seed(store, ids["to"], [("b|1", {"k": "X"}), ("b|2", {"k": "X"})])
    store.rebuild_links(ids["rel"], ids["from"], ids["to"],
                        [{"from_column": "k", "to_column": "k"}])

    out = store.links_of_object(ids["from"], "a|1")
    assert len(out) == 2
    assert all(x["direction"] == "out" for x in out)
    assert {x["peerKey"] for x in out} == {"b|1", "b|2"}

    # 反查：交货单只存了 in 方向，但要能查回合同
    back = store.links_of_object(ids["to"], "b|1")
    assert len(back) == 1
    assert back[0]["direction"] == "in"
    assert back[0]["peerType"] == ids["from"]
    assert back[0]["peerKey"] == "a|1"
    _cleanup(store, ids)


def test_links_of_object_unknown_key_is_empty(store, ids):
    _seed(store, ids["from"], [("a|1", {"k": "X"})])
    _seed(store, ids["to"], [("b|1", {"k": "X"})])
    store.rebuild_links(ids["rel"], ids["from"], ids["to"],
                        [{"from_column": "k", "to_column": "k"}])
    assert store.links_of_object(ids["from"], "不存在") == []
    _cleanup(store, ids)


# ── SQL 注入防护 ───────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "k'; DROP TABLE object_index; --",
    "k) = 'x' OR ('1",
    "a b",
    "",
])
def test_prop_expr_rejects_bad_column(bad):
    with pytest.raises(ValueError):
        _prop_expr("s", bad)


def test_prop_expr_ignores_unknown_transform():
    """未知变换当没写，不猜、不报错。"""
    assert _prop_expr("s", "col", "nonsense") == "s.props ->> 'col'"
