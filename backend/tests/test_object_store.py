"""对象存储（Postgres）的测试。

测的是换引擎时最容易踩的几件事：
    1. 增量 upsert —— 重复同步同一批数据不能报错，且要更新属性
    2. 水位线删除 —— 源表里消失的实例必须跟着消失，不能留陈旧行
    3. 分页下推 —— total 与 items 要对得上，深页也要能取到
    4. 等值筛选 —— JSONB @> 的语义（空串不参与、不存在的属性不匹配）
    5. 复合键 —— ``合同号|产品组合`` 这类键按不透明字符串处理

测试用 ``__test_`` 前缀的类型 ID 隔离，跑完自行清理；
连不上 Postgres 时整组跳过（不影响 CI 里没有 PG 的环境）。
"""
from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.services.object_store import PostgresObjectStore, get_object_store, reset_object_store


@pytest.fixture(scope="module")
def store() -> PostgresObjectStore:
    if not (settings.OBJECT_STORE_URL or "").strip():
        pytest.skip("未配置 OBJECT_STORE_URL")
    reset_object_store()
    s = get_object_store()
    if s is None:
        pytest.skip("对象存储不可用（Postgres 连不上）")
    yield s


@pytest.fixture()
def type_id() -> str:
    """每次测试用独立的类型 ID，互不干扰。"""
    return f"__test_{uuid.uuid4().hex[:10]}"


def _rows(*specs: tuple[str, str, dict]) -> list[dict]:
    return [
        {"__object_key__": key, "__title__": title, **props}
        for key, title, props in specs
    ]


def teardown_function() -> None:  # noqa: D401 - pytest hook
    pass


# ── 写入 / 增量 ───────────────────────────────────────────

def test_replace_type_inserts(store, type_id):
    n = store.replace_type(type_id, _rows(
        ("a", "甲", {"region": "山东"}),
        ("b", "乙", {"region": "山西"}),
    ), "src_tbl")
    assert n == 2
    assert store.query(type_id)["total"] == 2
    store.delete_type(type_id)


def test_replace_type_is_idempotent_and_updates(store, type_id):
    """同一批跑两次不报错；属性变化要生效（upsert 而不是跳过）。"""
    store.replace_type(type_id, _rows(("a", "甲", {"amount": "1"})), "t")
    store.replace_type(type_id, _rows(("a", "甲改", {"amount": "2"})), "t")

    page = store.query(type_id)
    assert page["total"] == 1
    assert page["items"][0]["title"] == "甲改"
    assert store.get_props(type_id, "a")["amount"] == "2"
    store.delete_type(type_id)


def test_stale_rows_are_removed(store, type_id):
    """源表里消失的行，下一轮同步必须清掉。"""
    store.replace_type(type_id, _rows(("a", "甲", {}), ("b", "乙", {})), "t")
    assert store.query(type_id)["total"] == 2

    store.replace_type(type_id, _rows(("a", "甲", {})), "t")
    assert store.query(type_id)["total"] == 1
    assert store.get_props(type_id, "b") is None
    store.delete_type(type_id)


def test_rows_without_key_are_skipped(store, type_id):
    n = store.replace_type(type_id, [
        {"__object_key__": None, "__title__": "无主键"},
        {"__object_key__": "ok", "__title__": "有主键"},
    ], "t")
    assert n == 1
    store.delete_type(type_id)


# ── 查询：分页 / 筛选 / 搜索 ──────────────────────────────

def test_pagination_is_consistent(store, type_id):
    rows = [(f"k{i:03d}", f"标题{i:03d}", {"idx": str(i)}) for i in range(120)]
    store.replace_type(type_id, _rows(*rows), "t")

    p1 = store.query(type_id, page=1, page_size=50)
    assert p1["total"] == 120
    assert len(p1["items"]) == 50

    p3 = store.query(type_id, page=3, page_size=50)
    assert len(p3["items"]) == 20

    # 深页也能取到，且不与第一页重叠
    deep = store.query(type_id, page=2, page_size=50)
    assert len(deep["items"]) == 50
    assert deep["items"][0]["objectKey"] != p1["items"][0]["objectKey"]
    store.delete_type(type_id)


def test_equality_filter_uses_jsonb(store, type_id):
    store.replace_type(type_id, _rows(
        ("a", "甲", {"region": "山东", "level": "A"}),
        ("b", "乙", {"region": "山西", "level": "A"}),
        ("c", "丙", {"region": "山东", "level": "B"}),
    ), "t")

    assert store.query(type_id, filter={"region": "山东"})["total"] == 2
    assert store.query(type_id, filter={"region": "山东", "level": "B"})["total"] == 1
    # 不存在的属性：不匹配，而不是全返回
    assert store.query(type_id, filter={"nope": "x"})["total"] == 0
    store.delete_type(type_id)


def test_empty_filter_value_is_ignored(store, type_id):
    """前端常传空串，空串不能变成 'AND props @> {}' 之外的条件。"""
    store.replace_type(type_id, _rows(("a", "甲", {"region": "山东"})), "t")
    assert store.query(type_id, filter={"region": ""})["total"] == 1
    assert store.query(type_id, filter={"region": None})["total"] == 1
    store.delete_type(type_id)


def test_search_matches_title_and_key(store, type_id):
    store.replace_type(type_id, _rows(
        ("DW2024001|1", "永春物流采购合同", {}),
        ("DW2024002|2", "宝祥运输合同", {}),
    ), "t")
    assert store.query(type_id, search="永春")["total"] == 1
    assert store.query(type_id, search="DW2024")["total"] == 2
    assert store.query(type_id, search="不存在")["total"] == 0
    store.delete_type(type_id)


def test_include_props_false_hides_properties(store, type_id):
    store.replace_type(type_id, _rows(("a", "甲", {"x": "1"})), "t")
    item = store.query(type_id, include_props=False)["items"][0]
    assert "properties" not in item
    assert item["objectKey"] == "a"
    store.delete_type(type_id)


# ── 复合键 ────────────────────────────────────────────────

def test_composite_object_key_is_opaque(store, type_id):
    """``合同号|产品组合`` 只当字符串，不当结构化路径。"""
    store.replace_type(type_id, _rows(
        ("DW001|111", "同合同-产品1", {"product": "111"}),
        ("DW001|222", "同合同-产品2", {"product": "222"}),
    ), "t")
    assert store.query(type_id)["total"] == 2
    assert store.get_props(type_id, "DW001|222")["product"] == "222"
    store.delete_type(type_id)


def test_rows_by_type_limits_per_type(store, type_id):
    other = type_id + "_b"
    store.replace_type(type_id, _rows(*[(f"k{i}", f"t{i}", {}) for i in range(10)]), "t")
    store.replace_type(other, _rows(*[(f"m{i}", f"u{i}", {}) for i in range(5)]), "t")

    out = store.rows_by_type(limit_per_type=3)
    assert len(out[type_id]) == 3
    assert len(out[other]) == 3
    store.delete_type(type_id)
    store.delete_type(other)
