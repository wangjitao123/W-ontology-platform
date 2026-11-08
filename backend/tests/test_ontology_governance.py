"""本体治理层测试：规则 / 动作 / 函数 / 版本。

这几张表之前全是空的，填内容的同时也修了发布流程的一个 bug：
`_sync_relations` 读 `v.entities` 恒为空（集合在本次请求里已被加载成空，
新增实体只进 session 不进集合），导致每个已发布版本的关系快照都是 0 条，
回滚时关系全丢。这里给它一条回归测试。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


# ── 规则：实例级评估 ────────────────────────────────────────

def test_rules_exist(db):
    from app.models.rule import BusinessRule

    rules = db.query(BusinessRule).filter(BusinessRule.status == "active").all()
    assert rules, "没有任何启用的规则"
    for r in rules:
        assert r.conditions_json, f"规则 {r.id} 没有 conditions_json，无法实例化评估"


def test_rules_use_real_properties(db):
    """规则引用的属性必须在对象类型上真实存在。

    早期版本引用过 overdue_days / aging_days 这种**不存在的派生列**，
    每次评估都让 Doris 报 Unknown column。
    """
    from app.models.entity import OntologyEntity
    from app.models.rule import BusinessRule

    for r in db.query(BusinessRule).filter(BusinessRule.status == "active").all():
        ent = db.get(OntologyEntity, r.entity_id) if r.entity_id else None
        assert ent is not None, f"规则 {r.id} 关联的对象类型不存在"
        names = {a.name for a in ent.attributes}
        for c in r.conditions_json or []:
            fld = str(c.get("field") or "")
            if "." in fld:
                _, prop = fld.split(".", 1)
                assert prop in names, (
                    f"规则「{r.name}」引用了 {ent.id} 上不存在的属性 {prop}"
                )


def test_instance_evaluation_runs(db):
    from app.services.rule_instance_evaluator import evaluate_all

    res = evaluate_all(db, sample=300)
    assert res["evaluated"] > 0
    for r in res["rules"]:
        assert r["total"] > 0 or r["note"], f"规则 {r['ruleName']} 既没样本也没说明"
        # 命中数不可能超过样本数
        assert 0 <= r["matched"] <= r["total"], f"{r['ruleName']} 命中数越界"


def test_instance_evaluation_matches_ground_truth(db):
    """抽一条阈值规则，用对象索引直接 count 对账。

    这是这套评估器唯一能证明"算得对"的方式——拿被测实现和
    另一条独立路径（直接 SQL）比。
    """
    from app.config import settings
    from app.services.object_store import get_object_store
    from app.services.rule_instance_evaluator import evaluate_all

    if not (settings.OBJECT_STORE_URL or "").strip():
        pytest.skip("未配置 OBJECT_STORE_URL")

    res = evaluate_all(db, sample=10_000, entity_id="ContractVehicle")
    stat = next((r for r in res["rules"] if r["entity"] == "ContractVehicle"), None)
    if stat is None or stat.get("note"):
        pytest.skip("ContractVehicle 规则或对象索引不可用")

    # 注意：object_index 在 **Postgres**，不在元枢的 SQLite 里。
    # 用 SQLAlchemy 的 db 去查会打到 SQLite 上（报 unrecognized token ":"）。
    from sqlalchemy import text as sa_text

    store = get_object_store()
    with store._engine.connect() as conn:  # noqa: SLF001
        row = conn.execute(sa_text(
            "SELECT count(*) FILTER (WHERE (props->>'actual_amount')::numeric > 10000000), "
            "       count(*) "
            "FROM object_index WHERE object_type_id = 'ContractVehicle'"
        )).first()
    if row is None or not row[1]:
        pytest.skip("对象索引里没有 ContractVehicle 实例")

    expected_matched, expected_total = int(row[0]), int(row[1])
    assert stat["matched"] == expected_matched, (
        f"命中数不一致：评估器 {stat['matched']} vs 直查 {expected_matched}"
    )
    assert stat["total"] == expected_total


# ── 函数：真的能执行 ────────────────────────────────────────

@pytest.mark.parametrize(
    ("fid", "params", "expect"),
    [
        ("fn_material_aging_grade", {"aging_days": 400}, "严重超期"),
        ("fn_material_aging_grade", {"aging_days": 200}, "超期"),
        ("fn_material_aging_grade", {"aging_days": 120}, "偏长"),
        ("fn_material_aging_grade", {"aging_days": 30}, "正常"),
        ("fn_contract_risk_level", {"amount": 60_000_000}, "高"),
        ("fn_contract_risk_level", {"amount": 20_000_000}, "中"),
        ("fn_contract_risk_level", {"amount": 1_000}, "低"),
        ("fn_ar_overdue_bucket", {"overdue_days": 200}, "181+"),
        ("fn_ar_overdue_bucket", {"overdue_days": 100}, "91-180"),
        ("fn_ar_overdue_bucket", {"overdue_days": 45}, "31-90"),
        ("fn_ar_overdue_bucket", {"overdue_days": 5}, "0-30"),
    ],
)
def test_functions_execute(db, fid, params, expect):
    from app.models.function import OntologyFunction
    from app.services.function_executor import FunctionExecutor

    func = db.get(OntologyFunction, fid)
    assert func is not None, f"函数 {fid} 不存在"
    out = FunctionExecutor(db).execute(func, params)
    assert out.success, f"{fid} 执行失败: {out.error}"
    assert str(out.value) == expect


def test_function_boundaries(db):
    """边界值：库龄正好 180 / 90 不应该被归到更大的一档。"""
    from app.models.function import OntologyFunction
    from app.services.function_executor import FunctionExecutor

    ex = FunctionExecutor(db)
    f = db.get(OntologyFunction, "fn_material_aging_grade")
    assert str(ex.execute(f, {"aging_days": 180}).value) == "偏长"   # >180 才算超期
    assert str(ex.execute(f, {"aging_days": 90}).value) == "正常"    # >90 才算偏长


# ── 动作：挂的是真实执行器 ──────────────────────────────────

def test_actions_reference_existing_functions(db):
    from app.models.function import OntologyFunction
    from app.models.rule import EntityAction

    acts = db.query(EntityAction).all()
    assert acts, "没有任何动作"
    fids = {f.id for f in db.query(OntologyFunction).all()}
    for a in acts:
        if a.action_type == "call_function":
            fid = (a.type_config or {}).get("function_id")
            assert fid in fids, f"动作 {a.id} 引用的函数 {fid} 不存在"


def test_notification_action_dry_run(db):
    """通知类动作不写数据，dry_run 必须成功且给出接收人。"""
    import asyncio

    from app.models.rule import EntityAction
    from app.services.action_executors import get_executor

    act = db.get(EntityAction, "act_ar_collection_notice")
    if act is None:
        pytest.skip("催收通知动作不存在")
    res = asyncio.run(get_executor("notification").execute(
        act.type_config or {},
        {"kunnr": "D1", "name1": "测试", "ksl": 1.0,
         "netdt": "2025-01-01", "belnr": "DOC1", "owner_email": "a@b.com"},
        dry_run=True,
    ))
    assert res.success, res.message
    assert "a@b.com" in res.message or "a@b.com" in str(res.output)


def test_call_function_action_really_executes(db):
    """call_function 动作要真的算出值，而不是返回一句 "Called function <id>"。"""
    import asyncio

    from app.models.rule import EntityAction
    from app.services.action_executors import get_executor

    act = db.get(EntityAction, "act_material_aging_grade")
    if act is None:
        pytest.skip("物料分级动作不存在")
    res = asyncio.run(get_executor("call_function").execute(
        act.type_config or {}, {"aging_days": 400}, dry_run=False,
    ))
    assert res.success, res.message
    assert (res.output or {}).get("value") == "严重超期", res.output


# ── 版本：发布流程与关系快照 ────────────────────────────────

def test_active_version_exists(db):
    from app.models.version import OntologyVersion

    v = db.query(OntologyVersion).filter(OntologyVersion.is_active == True).first()  # noqa: E712
    assert v is not None, "没有生效版本"
    assert v.status == "published"
    assert v.published_at is not None


def test_published_version_has_relation_snapshot(db):
    """回归：已发布版本的关系快照不能是 0 条。

    原先 `_sync_relations` 读 `v.entities`，而那集合在本次请求里已经被加载成空，
    新增的版本实体只进了 session，于是 source_ids 恒为空 → 直接 return，
    每个版本的关系快照都是 0，回滚时关系全丢。
    """
    from app.models.version import (
        OntologyVersion,
        OntologyVersionAttribute,
        OntologyVersionEntity,
        OntologyVersionRelation,
    )

    v = db.query(OntologyVersion).filter(OntologyVersion.is_active == True).first()  # noqa: E712
    if v is None:
        pytest.skip("没有生效版本")

    n_ent = db.query(OntologyVersionEntity).filter(
        OntologyVersionEntity.version_id == v.id).count()
    n_rel = db.query(OntologyVersionRelation).filter(
        OntologyVersionRelation.version_id == v.id).count()
    n_attr = (
        db.query(OntologyVersionAttribute)
        .join(OntologyVersionEntity,
              OntologyVersionAttribute.version_entity_id == OntologyVersionEntity.id)
        .filter(OntologyVersionEntity.version_id == v.id)
        .count()
    )
    assert n_ent > 0, "版本里没有实体快照"
    assert n_attr > 0, "版本里没有属性快照"
    assert n_rel > 0, (
        f"版本关系快照为 0（实体 {n_ent} 个）—— _sync_relations 又退回读 v.entities 了"
    )


def test_version_relation_endpoints_resolve(db):
    """关系快照的两端必须指向同一版本内的实体快照，否则回滚会生成悬空关系。"""
    from app.models.version import OntologyVersion, OntologyVersionEntity, OntologyVersionRelation

    v = db.query(OntologyVersion).filter(OntologyVersion.is_active == True).first()  # noqa: E712
    if v is None:
        pytest.skip("没有生效版本")
    ids = {ve.id for ve in db.query(OntologyVersionEntity).filter(
        OntologyVersionEntity.version_id == v.id).all()}
    for rel in db.query(OntologyVersionRelation).filter(
            OntologyVersionRelation.version_id == v.id).all():
        assert rel.from_version_entity_id in ids, f"关系 {rel.name} 起点悬空"
        assert rel.to_version_entity_id in ids, f"关系 {rel.name} 终点悬空"
