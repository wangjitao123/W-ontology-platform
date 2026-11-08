"""对象键构造的测试。

为什么单独测：真实业务表的粒度常常不是单列。
`dwd_crm_contract_vehicle_di` 是「合同 × 产品组合」，只用 contract_code 会撞
UNIQUE 约束、整批同步失败。这里固定住复合键的行为。
"""
from types import SimpleNamespace

from app.services.data_plane.object_index_service import (
    object_key_of,
    primary_key_of,
    primary_keys_of,
    title_property_of,
)


def _entity(config=None, attrs=None):
    return SimpleNamespace(
        config_json=config or {},
        attributes=attrs or [],
    )


def _attr(name, required=False):
    return SimpleNamespace(name=name, required=required)


# ── 主键解析 ──────────────────────────────────────────────

def test_single_key_from_config():
    e = _entity({"primary_key": "period"})
    assert primary_keys_of(e) == ["period"]
    assert primary_key_of(e) == "period"


def test_composite_key_from_config():
    e = _entity({"primary_key": ["contract_code", "product_portfolio_id"]})
    assert primary_keys_of(e) == ["contract_code", "product_portfolio_id"]
    # 兼容接口取第一列
    assert primary_key_of(e) == "contract_code"


def test_key_inferred_from_required_id_attribute():
    e = _entity({}, [_attr("name"), _attr("customer_id", required=True)])
    assert primary_keys_of(e) == ["customer_id"]


def test_key_falls_back_to_first_attribute():
    e = _entity({}, [_attr("code"), _attr("name")])
    assert primary_keys_of(e) == ["code"]


def test_key_falls_back_to_id_when_no_attributes():
    assert primary_keys_of(_entity({}, [])) == ["id"]


# ── 键值拼装 ──────────────────────────────────────────────

def test_object_key_single():
    e = _entity({"primary_key": "period"})
    assert object_key_of(e, {"period": "2026-07"}) == "2026-07"


def test_object_key_composite_joins_with_pipe():
    e = _entity({"primary_key": ["contract_code", "product_portfolio_id"]})
    row = {"contract_code": "DW20260528013", "product_portfolio_id": 4208727110417}
    assert object_key_of(e, row) == "DW20260528013|4208727110417"


def test_object_key_returns_none_when_part_missing():
    """主键列缺值就没有对象身份，必须返回 None 让调用方跳过该行。"""
    e = _entity({"primary_key": ["contract_code", "product_portfolio_id"]})
    assert object_key_of(e, {"contract_code": "X"}) is None
    assert object_key_of(e, {"contract_code": "X", "product_portfolio_id": None}) is None


def test_object_key_distinguishes_rows_sharing_first_column():
    """同一个合同号 + 不同产品组合 = 两个不同的对象，不能合并。"""
    e = _entity({"primary_key": ["contract_code", "product_portfolio_id"]})
    k1 = object_key_of(e, {"contract_code": "C1", "product_portfolio_id": 1})
    k2 = object_key_of(e, {"contract_code": "C1", "product_portfolio_id": 2})
    assert k1 != k2


# ── 标题列 ────────────────────────────────────────────────

def test_title_prefers_name_like_attribute():
    e = _entity({"primary_key": "customer_id"},
                [_attr("customer_id"), _attr("customer_name")])
    assert title_property_of(e) == "customer_name"


def test_title_falls_back_to_primary_key():
    e = _entity({"primary_key": "period"}, [_attr("period"), _attr("amount")])
    assert title_property_of(e) == "period"
