"""语义层（约束 / 证据 / 匹配）的测试。

重点测三件事：
    1. 约束的排序语义（block 优先）与过滤
    2. 证据引用完整性（约束引用的 evidence 必须真实存在）
    3. 按问题匹配的语义：关键词命中 / target 命中 / 都不命中就不返回

这些逻辑决定了"护栏会不会在正确的时候出现"——测不出来就会变成
要么永远不提示、要么每次都提示（后者等于噪声，用户会忽略）。
"""
from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models.semantic import SemanticConstraint, SemanticEvidence
from app.services import semantics_service


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    yield s
    s.close()


def test_constraints_seeded(db):
    rows = semantics_service.list_constraints(db)
    assert len(rows) >= 8, "语义层至少应有 8 条约束"
    # 每条都要有描述和建议——只有一句话的"约束"没有可操作性
    for r in rows:
        assert r["description"], f"{r['id']} 缺 description"
        assert r["remedy"], f"{r['id']} 缺 remedy"


def test_constraints_sorted_block_first(db):
    rows = semantics_service.list_constraints(db)
    sev = [r["severity"] for r in rows]
    rank = {"block": 0, "warn": 1, "info": 2}
    assert [rank[s] for s in sev] == sorted(rank[s] for s in sev), "block 必须排在前面"
    assert "block" in sev


def test_filter_by_target(db):
    rows = semantics_service.list_constraints(db, target_kind="entity", target_id="WmsAging")
    assert len(rows) == 1
    assert rows[0]["id"] == "sem_c_aging_snapshot_grain"
    assert rows[0]["severity"] == "block"


def test_filter_by_severity(db):
    rows = semantics_service.list_constraints(db, severity="block")
    assert all(r["severity"] == "block" for r in rows)
    assert len(rows) >= 5


def test_evidence_refs_all_resolve(db):
    """约束引用的证据必须全部存在——断链的证据等于没有证据。"""
    ev_ids = {e.id for e in db.query(SemanticEvidence).all()}
    assert ev_ids, "先跑 scripts/seed_semantic_layer.py"
    broken = []
    for c in db.query(SemanticConstraint).all():
        for ref in (c.evidence_ids or []):
            if ref not in ev_ids:
                broken.append((c.id, ref))
    assert not broken, f"证据引用断链: {broken}"


def test_every_block_constraint_has_evidence(db):
    """block 级约束必须有证据支撑，否则就是在拿猜测当硬规则。"""
    missing = [
        c.id
        for c in db.query(SemanticConstraint).filter(
            SemanticConstraint.severity == "block"
        ).all()
        if not (c.evidence_ids or [])
    ]
    assert not missing, f"block 约束缺证据: {missing}"


def test_list_constraints_includes_evidence(db):
    rows = semantics_service.list_constraints(db, target_id="WmsAging")
    assert rows[0]["evidence"], "约束详情应带上证据"
    assert rows[0]["evidence"][0]["result"]


# ── 按问题匹配 ────────────────────────────────────────────

def test_match_by_keyword(db):
    hits = semantics_service.relevant_constraints(db, "库龄超过180天的库存有多少")
    names = {h["id"] for h in hits}
    assert "sem_c_aging_snapshot_grain" in names
    hit = next(h for h in hits if h["id"] == "sem_c_aging_snapshot_grain")
    assert hit["matchedBy"] == "keyword"
    assert hit["matchedKeywords"]


def test_match_by_target(db):
    hits = semantics_service.relevant_constraints(
        db, "随便问一句不带关键词的话", targets=[("cube", "finance_summary")],
    )
    assert any(h["id"] == "sem_c_finance_company_level" for h in hits)
    hit = next(h for h in hits if h["id"] == "sem_c_finance_company_level")
    assert hit["matchedBy"] == "target"


def test_no_match_returns_empty(db):
    hits = semantics_service.relevant_constraints(db, "今天天气怎么样")
    assert hits == [], "不相关的问题不该硬塞护栏"


def test_match_limit(db):
    hits = semantics_service.relevant_constraints(db, "库存 库龄 合同 应收 客户 物料", limit=2)
    assert len(hits) <= 2


def test_block_ranked_before_warn(db):
    hits = semantics_service.relevant_constraints(db, "应收 账龄 库存")
    assert len(hits) >= 2
    rank = {"block": 0, "warn": 1, "info": 2}
    assert rank[hits[0]["severity"]] <= rank[hits[-1]["severity"]]


# ── 总览 ──────────────────────────────────────────────────

def test_overview_shape(db):
    o = semantics_service.overview(db)
    assert o["constraints"]["total"] >= 8
    assert o["evidence"]["total"] >= 10
    assert o["terms"]["entities"] == 14
    assert o["terms"]["withAliases"] == 14, "每个实体都应有别名"
    assert o["relations"]["bySemanticType"], "关系应标注语义类型"
