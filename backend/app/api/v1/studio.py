"""
本体工作室（OntologyStudio）后端接口 — 输出 v3.1 schema 格式
对齐 fttr-frontend 的 tbox/abox/rbox/capability/stats 数据契约

数据来源：OntologyEntity + EntityAttribute + EntityRelation + BusinessRule + EntityAction + OntologyFunction
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.entity import OntologyEntity, EntityAttribute
from app.models.relation import EntityRelation
from app.models.rule import BusinessRule, EntityAction
from app.models.function import OntologyFunction
from app.services.object_store import get_object_store

router = APIRouter(prefix="/studio", tags=["studio"])

logger = logging.getLogger(__name__)


_TIER_LABEL = {1: "核心", 2: "领域", 3: "场景"}
_SCHEMA_VERSION = "V3.1"

# 实例层上限：演示规模设计，防止一次把大表全拉进内存
_ABOX_MAX_ROWS = 1000
_ABOX_MAX_LINKS = 5000
_ABOX_MAX_INDIVIDUALS = 2000

# 实例层结果缓存：abox 每次要查 9 张物理表，而前端会轮询 + 多个接口同时命中，
# 源连接有 rate_limit_qps 限制（默认 20），不缓存会触发限流并静默丢数据。
# 实例数据是低频变化的，短 TTL 缓存既避免限流，也让 /studio 明显变快。
_ABOX_CACHE_TTL_SECONDS = 60.0
_abox_cache: tuple[float, list[dict[str, Any]], list[dict[str, Any]]] | None = None


# ─── TBox: 术语层 ────────────────────────────────────────────────────

@router.get("/tbox")
def get_tbox(db: Session = Depends(get_db)) -> dict[str, Any]:
    """T-box 术语层：所有对象类型 + 属性 + 关系类型的 schema 定义"""
    entities = db.query(OntologyEntity).all()
    relations = db.query(EntityRelation).all()

    entity_map = {e.id: e for e in entities}

    # 数据源索引：按主表名查 record_count
    from app.models.asset import Asset
    all_assets = db.query(Asset).filter(Asset.status == "active").all()
    ds_by_table = {(a.locator or {}).get("table", ""): a for a in all_assets if (a.locator or {}).get("table")}

    object_types = []
    for e in entities:
        properties = []
        for a in e.attributes:
            properties.append({
                "apiName": a.name,
                "displayName": a.name,
                "dataType": _normalize_data_type(a.type),
                "physicalName": a.source_field or a.name,
                "required": a.required,
                "isDerived": False,
                "description": a.description or "",
                "valueConstraint": a.constraints_json,
                "enum": None,
                "sourceColumn": a.source_field,
                "sourceTable": a.source_table,
                "dataStatus": a.data_status,
            })

        # 实例数：从主映射表的 asset.profile.row_count 推算
        primary_tables = [a.source_table for a in e.attributes if a.source_table]
        abox_scale = 0
        if primary_tables:
            ds = ds_by_table.get(primary_tables[0])
            if ds:
                abox_scale = (ds.profile or {}).get("row_count", 0) or 0

        object_types.append({
            "apiName": e.name,
            "displayName": e.name_cn,
            "id": e.id,
            # entity（身份稳定）/ snapshot（事实快照）/ report（聚合报表）。
            # 前端据此分组：不该把报表和实体混在一张图里。
            "objectKind": (e.config_json or {}).get("object_kind") or "entity",
            "objectKindLabel": (e.config_json or {}).get("object_kind_label"),
            "primaryKey": _primary_key_of(e),
            "titleProperty": _title_property_of(e),
            "tier": e.tier,
            "scenarioCode": _scenario_of(e),
            "status": e.status,
            "visibility": "PROMINENT",
            "iriPattern": f"ontology:{e.name}/{{id}}",
            "aboxScale": abox_scale,
            "dataSource": _data_source_of(e),
            "description": e.description or "",
            "remarks": [],
            "properties": properties,
            "ruleCount": len(e.rules),
            "actionCount": len(e.actions),
            "functionCount": len(e.functions),
        })

    link_types = []
    for r in relations:
        src = entity_map.get(r.from_entity_id)
        tgt = entity_map.get(r.to_entity_id)
        if not src or not tgt:
            continue
        link_types.append({
            "apiName": r.name,
            "displayName": r.description or r.name,
            "source": src.name,
            "target": tgt.name,
            "cardinality": r.cardinality,
            "relType": r.rel_type,
            "acyclic": r.acyclic,
            "description": r.description or "",
            # 实例层遍历依据；为空表示这条关系只能画在 Schema 层
            "joinKeys": r.join_keys or [],
        })

    tier_breakdown = {f"Tier{t}": [e.name for e in entities if e.tier == t] for t in (1, 2, 3)}
    prop_count = sum(len(e.attributes) for e in entities)

    return {
        "kind": "tbox",
        "version": _SCHEMA_VERSION,
        "generatedAt": _now_iso(),
        "source": "ontology_entities + entity_attributes + entity_relations",
        "meta": {
            "objectTypeCount": len(entities),
            "propertyCount": prop_count,
            "linkTypeCount": len(link_types),
            "scenarios": _all_scenarios(entities),
            "tierBreakdown": tier_breakdown,
        },
        "objectTypes": object_types,
        "linkTypes": link_types,
    }


# ─── ABox: 断言层 ────────────────────────────────────────────────────

@router.get("/abox")
def get_abox(db: Session = Depends(get_db)) -> dict[str, Any]:
    """A-box 断言层：基于属性映射推算实例数 + 字段覆盖率"""
    _t0 = time.perf_counter()
    entities = db.query(OntologyEntity).all()
    _t1 = time.perf_counter()

    # 一次性加载所有 asset，按 table_name 建索引
    from app.models.asset import Asset
    all_assets = db.query(Asset).filter(Asset.status == "active").all()
    ds_by_table: dict[str, Asset] = {}
    for a in all_assets:
        tbl = (a.locator or {}).get("table")
        if tbl:
            ds_by_table[tbl] = a

    hydration = []
    individuals_total = 0
    by_object: dict[str, int] = {}
    for e in entities:
        # 同一对象的属性可能映射到多个表，取第一张表的 record_count 当实例数
        mapped_attrs = [a for a in e.attributes if a.source_field]
        populated_attrs = [a for a in mapped_attrs if a.data_status == "已确认"]
        primary_table: str | None = None
        instance_count = 0
        backing_source = ""
        if mapped_attrs:
            tables = [a.source_table for a in mapped_attrs if a.source_table]
            if tables:
                primary_table = tables[0]
                ds = ds_by_table.get(primary_table)
                if ds:
                    instance_count = (ds.profile or {}).get("row_count", 0) or 0
                    backing_source = ds.name
                else:
                    backing_source = primary_table

        # 水合等级
        total = len(e.attributes)
        if not mapped_attrs:
            level = "none"
        elif primary_table and instance_count > 0 and len(populated_attrs) == total:
            level = "full"
        elif primary_table and instance_count > 0:
            level = "partial"
        else:
            level = "mapping"

        coverage = len(mapped_attrs) / total if total > 0 else 0
        hydration.append({
            "objectTypeApiName": e.name,
            "level": level,
            "instanceCount": instance_count,
            "propertyCompleteness": {
                "total": total,
                "mapped": len(mapped_attrs),
                "populated": len(populated_attrs),
                "coverage": round(coverage, 4),
            },
            "backingSource": backing_source,
            "primaryTable": primary_table,
        })
        individuals_total += instance_count
        by_object[e.name] = instance_count

    # ── 实例层：拉真实行 + 按 join_keys 连边 ─────────────────────────
    individuals, links = _instance_layer(db, entities)
    _t2 = time.perf_counter()
    # 仅在偏慢时留痕（项目未配置 logging.basicConfig，root 默认 WARNING，info 会被丢弃）
    if (_t2 - _t0) > 0.5:
        logger.warning(
            "abox 耗时: 查实体 %.0fms + 实例层 %.0fms = %.0fms",
            (_t1 - _t0) * 1000, (_t2 - _t1) * 1000, (_t2 - _t0) * 1000,
        )

    return {
        "kind": "abox",
        "version": _SCHEMA_VERSION,
        "generatedAt": _now_iso(),
        "source": "real rows via attribute.source_table + relation.join_keys",
        "meta": {
            "individualCount": individuals_total,
            "linkCount": len(links),
            "individualsReturned": len(individuals),
            "linksReturned": len(links),
            "scenarios": _all_scenarios(entities),
            "byObjectType": by_object,
        },
        "individuals": individuals,
        "links": links,
        "hydration": hydration,
    }


@router.get("/instance/{object_type}/{instance_id}")
def get_instance_neighbors(
    object_type: str, instance_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """取单个实例的一跳邻居。

    `instance_id` 是实例的主键值（不是拼接后的 "Type:value"），
    返回该实例本身、与它直接相连的实例，以及它们之间的连线。
    """
    entities = db.query(OntologyEntity).all()
    individuals, links = _instance_layer(db, entities)

    node_id = f"{object_type}:{instance_id}"
    by_id = {i["id"]: i for i in individuals}
    if node_id not in by_id:
        return {
            "nodeId": node_id,
            "found": False,
            "node": None,
            "neighbors": [],
            "links": [],
        }

    touched = [
        lk for lk in links
        if lk["source"] == node_id or lk["target"] == node_id
    ]
    neighbor_ids = set()
    for lk in touched:
        neighbor_ids.add(lk["source"] if lk["target"] == node_id else lk["target"])
    neighbor_ids.discard(node_id)

    return {
        "nodeId": node_id,
        "found": True,
        "node": by_id[node_id],
        "neighbors": [by_id[n] for n in sorted(neighbor_ids) if n in by_id],
        "links": touched,
    }


# ─── 实例级连线（Object Link）──────────────────────────────────────

@router.get("/links")
def list_links(db: Session = Depends(get_db)) -> dict[str, Any]:
    """关系定义 + 每条关系当前物化了多少条实例连线。

    与 `/tbox` 的 linkTypes 区别：那里只输出类型级定义（join 键、基数），
    这里额外带上**实例层的实际边数**，能立刻看出哪条关系是空的。
    """
    from app.services.object_link_service import definitions

    rows = definitions(db)
    return {
        "kind": "object_links",
        "totalRelations": len(rows),
        "totalLinks": sum(r["linkCount"] for r in rows),
        "relations": rows,
    }


@router.post("/links/rebuild")
def rebuild_links(db: Session = Depends(get_db)) -> dict[str, Any]:
    """按 join_keys 重新物化所有关系的实例连线。

    对象索引同步后会自动调；手动改动 join_keys 之后也应跑一次，
    否则界面上看到的还是旧边。
    """
    from app.services.object_link_service import rebuild_all

    return rebuild_all(db)


# ─── 对象索引（Object Index）────────────────────────────────────────

@router.post("/index/sync")
def sync_object_index(db: Session = Depends(get_db)) -> dict[str, Any]:
    """全量重建对象索引。

    索引是对象实例的物化副本，源表仍是唯一真相；同步后 /studio/abox
    与 /object-sets/* 都从索引读，不再每次回源查表。
    """
    from app.services.data_plane.object_index_service import ObjectIndexService

    result = ObjectIndexService(db).sync_all()
    # 索引变了，实例层缓存必须失效
    global _abox_cache
    _abox_cache = None
    return result


@router.get("/index/stats")
def object_index_stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    """对象索引的规模与每个类型的同步状态。"""
    from app.services.data_plane.object_index_service import ObjectIndexService

    return ObjectIndexService(db).stats()


# ─── RBox: 规则层 ────────────────────────────────────────────────────
@router.get("/rbox")
def get_rbox(db: Session = Depends(get_db)) -> dict[str, Any]:
    """R-box 规则层：业务规则定义"""
    rules = db.query(BusinessRule).all()
    entities = db.query(OntologyEntity).all()
    entity_map = {e.id: e for e in entities}

    rule_list = []
    for r in rules:
        ent = entity_map.get(r.entity_id) if r.entity_id else None
        meta = r.rule_meta_json or {}
        category = (meta.get("category") if isinstance(meta, dict) else None) or "INFERENCE"
        rule_list.append({
            "rule_id": r.id,
            "display_name": r.name,
            "category": category,
            "priority": r.priority,
            "condition": r.conditions_json or r.condition_expr,
            "action": {
                "reason": r.action_desc,
                "type": "annotate",
            },
            "applicable_objects": [ent.name] if ent else [],
            "scenarioCode": _scenario_of(ent) if ent else "core",
            "status": r.status,
            "trigger_count": r.trigger_count,
        })

    family_summary: dict[str, int] = {}
    for r in rule_list:
        family = r["category"]
        family_summary[family] = family_summary.get(family, 0) + 1

    return {
        "kind": "rbox",
        "version": _SCHEMA_VERSION,
        "generatedAt": _now_iso(),
        "source": "business_rules",
        "meta": {
            "ruleFamilyCount": len(family_summary),
            "ruleCount": len(rule_list),
            "scenarios": _all_scenarios(entities),
        },
        "rules": rule_list,
        "ruleFamilies": [
            {"familyId": k, "displayName": k, "ruleCount": v} for k, v in family_summary.items()
        ],
        "constraints": [],
    }


# ─── Capability: 行为层（actions + functions） ────────────────────────

@router.get("/capability")
def get_capability(db: Session = Depends(get_db)) -> dict[str, Any]:
    actions = db.query(EntityAction).all()
    functions = db.query(OntologyFunction).all()
    entities = db.query(OntologyEntity).all()
    entity_map = {e.id: e for e in entities}

    action_list = []
    for a in actions:
        ent = entity_map.get(a.entity_id)
        meta = a.action_meta_json or {}
        desc = (meta.get("description") if isinstance(meta, dict) else None) or ""
        action_list.append({
            "apiName": f"{ent.name}.{a.name}" if ent else a.name,
            "shortApiName": a.name,
            "displayName": a.name,
            "boundObjectType": ent.name if ent else None,
            "scenarioCode": _scenario_of(ent) if ent else "core",
            "kind": "ACTION",
            "returnType": "void",
            "inputProperties": [p.get("name") for p in (a.parameters_json or []) if isinstance(p, dict)],
            "outputProperties": [e.get("property") for e in (a.effects_json or []) if isinstance(e, dict)],
            "writesState": True,
            "implRef": f"action_executor::{a.name}",
            "description": desc,
            # EntityAction 没有 `type`；分类字段是 `action_type`
            # （notification / call_function / sql_exec / api_call ...）。
            # entity_actions 长期为 0 行，所以这行从未被执行过，
            # 一旦有动作就 AttributeError → 整个 capability 接口 500。
            "actionType": a.action_type,
        })

    function_list = []
    for f in functions:
        ent = entity_map.get(f.entity_id) if f.entity_id else None
        function_list.append({
            "apiName": f"{ent.name}.{f.name}" if ent else f.name,
            "shortApiName": f.name,
            "displayName": f.name,
            "boundObjectType": ent.name if ent else None,
            "scenarioCode": _scenario_of(ent) if ent else "core",
            "kind": "FUNCTION",
            "returnType": f.return_type or "string",
            "isDerived": f.is_derived_property,
            "logicType": f.logic_type,
            "description": f.description or "",
        })

    return {
        "kind": "capability",
        "version": _SCHEMA_VERSION,
        "generatedAt": _now_iso(),
        "source": "entity_actions + ontology_functions",
        "meta": {
            "actionCount": len(action_list),
            "functionCount": len(function_list),
            "skillCount": 0,
            "toolCount": 0,
            "modelCount": 0,
            "scenarios": _all_scenarios(entities),
        },
        "actions": action_list,
        "functions": function_list,
        "skills": [],
        "tools": [],
        "models": [],
    }


# ─── Stats: 汇总指标 ─────────────────────────────────────────────────

@router.get("/stats")
def get_stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    entities = db.query(OntologyEntity).all()
    relations = db.query(EntityRelation).all()
    rules = db.query(BusinessRule).all()
    actions = db.query(EntityAction).all()
    functions = db.query(OntologyFunction).all()

    from app.models.asset import Asset
    all_assets = db.query(Asset).filter(Asset.status == "active").all()
    ds_by_table = {(a.locator or {}).get("table", ""): a for a in all_assets if (a.locator or {}).get("table")}

    prop_count = sum(len(e.attributes) for e in entities)

    tier_breakdown = {f"Tier{t}": [e.name for e in entities if e.tier == t] for t in (1, 2, 3)}
    scenario_breakdown: dict[str, int] = {}
    for e in entities:
        sc = _scenario_of(e)
        scenario_breakdown[sc] = scenario_breakdown.get(sc, 0) + 1

    rule_family: dict[str, int] = {}
    for r in rules:
        meta = r.rule_meta_json or {}
        family = (meta.get("category") if isinstance(meta, dict) else None) or "INFERENCE"
        rule_family[family] = rule_family.get(family, 0) + 1

    # ABox 真实数据
    abox_total = 0
    abox_by_object: dict[str, int] = {}
    abox_by_scenario: dict[str, int] = {}
    for e in entities:
        primary_tables = [a.source_table for a in e.attributes if a.source_table]
        count = 0
        if primary_tables:
            ds = ds_by_table.get(primary_tables[0])
            if ds:
                count = (ds.profile or {}).get("row_count", 0) or 0
        abox_total += count
        abox_by_object[e.name] = count
        sc = _scenario_of(e)
        abox_by_scenario[sc] = abox_by_scenario.get(sc, 0) + count

    return {
        "generatedAt": _now_iso(),
        "elapsedSeconds": 0,
        "tbox": {
            "objectTypeCount": len(entities),
            "propertyCount": prop_count,
            "linkTypeCount": len(relations),
            "tierBreakdown": tier_breakdown,
            "scenarioBreakdown": scenario_breakdown,
        },
        "capability": {
            "actionCount": len(actions),
            "functionCount": len(functions),
            "skillCount": 0,
            "toolCount": 0,
            "modelCount": 0,
            "scenarioBreakdown": scenario_breakdown,
        },
        "rbox": {
            "ruleFamilyCount": len(rule_family),
            "ruleCount": len(rules),
            "byFamily": rule_family,
            "scenarioBreakdown": scenario_breakdown,
        },
        "abox": {
            "individualCount": abox_total,
            "linkCount": 0,
            "byObjectType": abox_by_object,
            "byScenario": abox_by_scenario,
            "bySourceRef": {},
        },
        "validation": {
            "passed": 0,
            "failed": 0,
            "checks": [],
        },
    }


# ─── 实时事件流：聚合 traces + audit_log + 规则触发 ──────────────────

@router.get("/events")
def get_events(limit: int = 30, db: Session = Depends(get_db)) -> dict[str, Any]:
    """聚合本体平台的实时事件，供数字孪生视图的事件流卡片消费"""
    from app.models.trace import AgentTrace
    from app.models.audit import AuditLog
    from app.models.agent import Agent

    events: list[dict[str, Any]] = []

    # 1. Agent traces（智能体调用记录）
    traces = db.query(AgentTrace).order_by(AgentTrace.created_at.desc()).limit(limit).all()
    agent_names = {a.id: a.name for a in db.query(Agent).all()}
    for t in traces:
        events.append({
            "id": f"trace-{t.id}",
            "ts": t.created_at.isoformat() if t.created_at else "",
            "type": "skill" if t.status == "ok" else "alert",
            "icon": "✦" if t.status == "ok" else "▲",
            "desc": f"{agent_names.get(t.agent_id, '智能体')} {'完成' if t.status == 'ok' else '异常'}: {(t.output_text or t.input_text or '')[:60]}",
        })

    # 2. AuditLog（实体/规则/动作的 CRUD）
    audits = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
    for a in audits:
        type_map = {"entity": "state_change", "rule": "rule", "action": "skill", "agent": "skill"}
        icon_map = {"entity": "●", "rule": "◇", "action": "✦", "agent": "▸"}
        action_label = {"create": "创建", "update": "更新", "delete": "删除", "publish": "发布"}.get(a.action, a.action)
        events.append({
            "id": f"audit-{a.id}",
            "ts": a.timestamp.isoformat() if a.timestamp else "",
            "type": type_map.get(a.target_type, "state_change"),
            "icon": icon_map.get(a.target_type, "●"),
            "desc": f"{a.user_name or '系统'} {action_label} {a.target_type} {a.target_name}",
        })

    # 3. 高频触发规则（trigger_count Top N）
    rules = (
        db.query(BusinessRule)
        .filter(BusinessRule.trigger_count > 0)
        .filter(BusinessRule.last_triggered.isnot(None))
        .order_by(BusinessRule.last_triggered.desc())
        .limit(10)
        .all()
    )
    for r in rules:
        if r.last_triggered:
            events.append({
                "id": f"rule-{r.id}",
                "ts": r.last_triggered.isoformat(),
                "type": "rule",
                "icon": "◇",
                "desc": f"规则 {r.name} 触发 (累计 {r.trigger_count} 次)",
            })

    # 时间倒序合并裁剪
    events.sort(key=lambda e: e["ts"], reverse=True)
    return {
        "generatedAt": _now_iso(),
        "total": len(events),
        "events": events[:limit],
    }


# ─── AI 业务解读（OntologyNarrator）─────────────────────────────────

class NarratorRequest(BaseModel):
    apiName: str
    displayName: str
    tier: int
    instanceCount: int = 0
    propCount: int = 0
    relCount: int = 0
    ruleCount: int = 0
    actionCount: int = 0
    kpiSummary: str = ""


# 14 个核心对象的 hardcoded fallback 文案（LLM 不可用时用）
_NARRATOR_FALLBACK = {
    "Customer": "客户是整个业务的核心主体。客户与订单、产品、地址、分群都有直接关联，是所有推理链的起点。建议关注 ARPU 中价值区间——这是转化率最高的黄金区间。",
    "Order": "订单记录了客户的每一笔交易行为。订单连接了客户和产品，是衡量策略效果的核心指标。通过分析订单数据，可以追溯哪个渠道、哪个话术、哪个产品组合带来了最高转化。",
    "Product": "产品是策略的核心载体。低门槛产品更容易促成转化。产品与客户分群通过匹配规则关联，不同分群推荐不同产品。",
    "InstallOrder": "装机订单是宽带退单稽核的关键起点。它连接了客户、地址、工程师、派单记录，是归因分析的核心证据来源。",
    "InstallChurn": "装机退单是退单稽核的核心对象。通过 35 项证据推理（E1-E35），系统自动给出归因结论：客户责任 / 工程师责任 / 业务责任 / 第三方责任 / 异常情况。",
    "DispatchRecord": "派单记录承载了工程师与工单的派遣关系，是工程师责任归因的核心证据。",
    "EngineerCall": "工程师与客户的通话录音，经过语音质检后产出客户意向、工程师服务质量等关键证据。",
    "Channel": "渠道定义了触达通路。不同渠道的转化率和覆盖面差异显著。当前策略是多渠道协同——先线上预热，再线下跟进。",
    "Staff": "员工是触达和服务的核心执行力量。客户经理人均管户百余户，他们的执行效率直接影响转化。",
    "RegionalOrg": "组织架构覆盖各盟市分公司。不同分公司的渗透率和转化率差异显著，需要将经验向其他盟市推广。",
}


@router.post("/narrator/explain")
def narrator_explain(req: NarratorRequest) -> dict[str, Any]:
    """对本体对象生成业务解读（fttr OntologyNarrator 复刻）"""
    tier_label = {1: "核心层", 2: "领域层", 3: "场景层"}.get(req.tier, "未知层")
    prompt = f"""你是 Agentic Ontology 智能本体平台的业务解说员。请用简洁生动的中文，对以下本体对象进行全方位业务解读。

对象：{req.displayName}（{req.apiName}）
层级：Tier {req.tier}（{tier_label}）
实例数：{req.instanceCount}｜属性数：{req.propCount}｜关系数：{req.relCount}｜规则数：{req.ruleCount}｜动作数：{req.actionCount}
业务指标：{req.kpiSummary or '暂无'}

请从以下维度解读（每个维度 1-2 句话，总共不超过 150 字）：
1. 业务角色：这个对象在业务中扮演什么角色？
2. 关键价值：它的核心业务价值是什么？
3. 当前状态：基于数据，当前业务状态如何？
4. 关联影响：它与哪些对象的关系最关键？
5. 推荐关注：当前最需要关注什么？

用口语化的方式，像一个资深业务专家在给新同事介绍。不要用 markdown 格式。"""

    try:
        from app.services.copilot import get_llm_client
        client = get_llm_client()
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300,
        )
        content = response.choices[0].message.content or ""
        # 剥离推理模型的 <think>...</think> 块
        import re
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
        if content:
            return {"source": "llm", "content": content}
    except Exception as e:
        print(f"narrator LLM failed: {e}")

    # Fallback 到 hardcoded 模板
    fallback = _NARRATOR_FALLBACK.get(
        req.apiName,
        f"{req.displayName} 是本体中的{tier_label}对象，当前有 {req.instanceCount} 个实例。"
        f"它在业务中承担重要角色，与多个对象存在关联关系。"
        f"建议点击查看详情面板了解更多属性和规则信息。"
    )
    return {"source": "fallback", "content": fallback}


# ─── 数据刷新：手动触发所有 asset 实例计数 ──────────────────────
def refresh_counts(db: Session = Depends(get_db)) -> dict[str, Any]:
    """触发所有启用的数据资产重新统计 row_count，刷新 A-Box 实例数"""
    from app.models.asset import Asset
    from app.services.data_plane.entity_data_service import EntityDataService

    assets = db.query(Asset).filter(Asset.status == "active", Asset.kind.in_(["table", "sql_view"])).all()
    results = []
    success = 0
    svc = EntityDataService(db)
    for asset in assets:
        table_name = (asset.locator or {}).get("table")
        if not table_name:
            results.append({"id": asset.id, "name": asset.name, "status": "skip", "message": "未关联数据表"})
            continue
        r = svc.execute_sql_on_asset(asset.name, f"SELECT COUNT(*) FROM `{table_name}`", purpose="refresh_counts")
        if r.get("error"):
            results.append({"id": asset.id, "name": asset.name, "status": "error", "message": r["error"]})
            continue
        count = r["rows"][0][0] if r.get("rows") else 0
        profile = asset.profile or {}
        profile["row_count"] = count
        asset.profile = profile
        success += 1
        results.append({"id": asset.id, "name": asset.name, "status": "ok", "count": count})
    db.commit()
    return {
        "refreshedAt": _now_iso(),
        "total": len(assets),
        "success": success,
        "results": results,
    }


# ─── 内部辅助 ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scenario_of(e: OntologyEntity | None) -> str:
    """从 entity 的 publish_config / config_json 中提取场景代码，缺省按 tier 推断"""
    if not e:
        return "core"
    cfg = e.publish_config or {}
    if isinstance(cfg, dict) and cfg.get("scenarioCode"):
        return cfg["scenarioCode"]
    sj = e.config_json or {}
    if isinstance(sj, dict) and sj.get("scenarioCode"):
        return sj["scenarioCode"]
    return "core" if e.tier in (1, 2) else "s1"


def _all_scenarios(entities: list[OntologyEntity]) -> list[str]:
    return sorted({_scenario_of(e) for e in entities})


def _primary_key_of(e: OntologyEntity) -> str:
    for a in e.attributes:
        if a.required and a.name.endswith("_id"):
            return a.name
    if e.attributes:
        return e.attributes[0].name
    return "id"


def _title_property_of(e: OntologyEntity) -> str:
    for a in e.attributes:
        if "name" in a.name.lower() or "title" in a.name.lower():
            return a.name
    return _primary_key_of(e)


def _data_source_of(e: OntologyEntity) -> str:
    tables = sorted({a.source_table for a in e.attributes if a.source_table})
    return " + ".join(tables) if tables else ""


def _normalize_data_type(t: str | None) -> str:
    if not t:
        return "String"
    return {
        "string": "String", "number": "Number", "boolean": "Boolean",
        "date": "Date", "json": "Json", "ref": "Reference",
        "computed": "Computed", "enum": "Enum",
    }.get(t.lower(), t.capitalize())


# ─── 实例层（A-box）─────────────────────────────────────────────────

def _same_value(a: Any, b: Any) -> bool:
    """实例连线用的等值比较：NULL 不参与匹配，其余按字符串比。"""
    if a is None or b is None:
        return False
    return str(a) == str(b)


def _fetch_all_rows(db: Session, asset: Any) -> list[dict[str, Any]]:
    """取一张物理表的行（上限 _ABOX_MAX_ROWS）。

    标识符引号必须跟随连接方言：MySQL 用反引号，PostgreSQL 用双引号。
    写死反引号会让 PG 连接的 AST 安全审查 ParseError(ast_failed)。
    """
    from app.connectors import ConnectorRegistry
    from app.models.connection import Connection
    from app.services.data_plane.execute_service import ExecuteRequest, ExecuteService

    table = (asset.locator or {}).get("table") or ""
    if not table:
        return []

    conn = db.get(Connection, asset.connection_id) if asset.connection_id else None
    quote = ConnectorRegistry.get(conn.type).quote_identifier() if conn else '"'
    sql = f"SELECT * FROM {quote}{table}{quote} LIMIT {_ABOX_MAX_ROWS}"
    started = time.perf_counter()
    try:
        result = ExecuteService(db).execute(
            ExecuteRequest(asset_id=asset.id, sql=sql, purpose="studio.abox")
        )
    except Exception:
        logger.exception("abox: 读取表 %s 失败，跳过该对象的实例", table)
        return []
    elapsed = time.perf_counter() - started
    # 单表超过 1s 说明源库/连接池在排队；留痕便于定位（正常应 <100ms）
    if elapsed > 1.0:
        logger.warning("abox: 读取表 %s 耗时 %.2fs（异常偏慢）", table, elapsed)

    columns = result.columns or []
    return [dict(zip(columns, row)) for row in (result.rows or [])]


def _build_instance_layer(
    db: Session, entities: list[OntologyEntity]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """组装实例节点与实例级连线。

    优先读对象索引（object_index，本地元数据库）；索引为空时回退到直接查源表。

    连线用 relation.join_keys 在内存里做等值匹配，不发跨表 SQL JOIN：
    ExecuteService 的白名单只覆盖单个 Asset 的主表，跨 Asset JOIN 会被拦。
    演示规模（每表 ≤ 1000 行）下内存匹配完全够用，且方言无关。
    """
    from app.models.asset import Asset
    from app.services.data_plane.object_index_service import (
        ObjectIndexService, object_key_of,
    )

    # 索引存在就用索引，免去每次回源（也避开连接的 rate_limit_qps）
    index_svc = ObjectIndexService(db)
    # 每个类型只取展示上限那么多行：索引可能有几万行（真实数仓），
    # 全量加载会让 abox 从毫秒级退化到两秒级。
    per_type = max(1, _ABOX_MAX_INDIVIDUALS // max(1, len(entities)))
    indexed = index_svc.rows_by_type(limit_per_type=per_type) if index_svc.has_data() else {}

    assets_by_table: dict[str, Asset] = {}
    if not indexed:
        for a in db.query(Asset).filter(Asset.status == "active").all():
            tbl = (a.locator or {}).get("table")
            if tbl:
                assets_by_table[tbl] = a

    individuals: list[dict[str, Any]] = []
    # 对象名 -> (行列表, 主键列名)
    rows_by_entity: dict[str, tuple[list[dict[str, Any]], str]] = {}

    for e in entities:
        rows = indexed.get(e.id)
        if rows is None:
            # 该类型没有索引 → 回源
            mapped = [a for a in e.attributes if a.source_table]
            if not mapped:
                continue
            asset = assets_by_table.get(mapped[0].source_table)
            if not asset:
                continue
            rows = _fetch_all_rows(db, asset)
        if not rows:
            continue

        pk = _primary_key_of(e)
        title_prop = _title_property_of(e)
        rows_by_entity[e.name] = (rows, pk)

        for row in rows:
            if len(individuals) >= _ABOX_MAX_INDIVIDUALS:
                break
            # 用 object_key_of 支持复合主键（如 合同 × 产品组合）
            raw_id = object_key_of(e, row)
            individuals.append({
                "id": f"{e.name}:{raw_id}",
                "objectType": e.name,
                "objectTypeDisplay": e.name_cn,
                "title": str(row.get(title_prop) or raw_id or ""),
                "primaryKeyValue": None if raw_id is None else str(raw_id),
                "properties": {
                    k: (None if v is None else str(v)) for k, v in row.items()
                },
            })

    entity_map = {e.id: e for e in entities}
    links: list[dict[str, Any]] = []

    # 优先用物化连线（Postgres object_link）。旧实现要在内存里做
    # 源类型 × 目标类型的两两比较：WmsAging(1.3 万) × B2bAging(2.9 万)
    # 就是 3.7 亿次，实际跑不动，等于一条边也画不出来。
    store = get_object_store()
    if store is not None:
        from app.services.object_link_service import definitions as _link_defs

        rel_meta = {r["id"]: r for r in _link_defs(db)}
        # 节点集被 per_type 截断过，而连线是全量的：只画两端都在图上的边，
        # 否则前端会拿到一堆指向不存在节点的悬空边。
        node_ids = {i["id"] for i in individuals}
        for row in store.links_by_type_pair():
            if len(links) >= _ABOX_MAX_LINKS:
                break
            src_id = f"{row['fromType']}:{row['fromKey']}"
            tgt_id = f"{row['toType']}:{row['toKey']}"
            if src_id not in node_ids or tgt_id not in node_ids:
                continue
            meta = rel_meta.get(row["relId"]) or {}
            links.append({
                "id": f"{row['relId']}:{row['fromKey']}:{row['toKey']}",
                "linkType": meta.get("name") or row["relId"],
                "label": meta.get("displayName") or meta.get("name") or row["relId"],
                "source": src_id,
                "target": tgt_id,
                "sourceType": row["fromType"],
                "targetType": row["toType"],
            })
        return individuals, links

    for rel in db.query(EntityRelation).all():
        pairs = [
            (jk.get("from_column"), jk.get("to_column"))
            for jk in (rel.join_keys or [])
            if isinstance(jk, dict) and jk.get("from_column") and jk.get("to_column")
        ]
        if not pairs:
            continue

        src = entity_map.get(rel.from_entity_id)
        tgt = entity_map.get(rel.to_entity_id)
        if not src or not tgt:
            continue
        src_rows = rows_by_entity.get(src.name)
        tgt_rows = rows_by_entity.get(tgt.name)
        if not src_rows or not tgt_rows:
            continue

        s_list, s_pk = src_rows
        t_list, t_pk = tgt_rows
        for srow in s_list:
            if len(links) >= _ABOX_MAX_LINKS:
                break
            for trow in t_list:
                if all(_same_value(srow.get(fc), trow.get(tc)) for fc, tc in pairs):
                    links.append({
                        "id": f"{rel.name}:{srow.get(s_pk)}:{trow.get(t_pk)}",
                        "linkType": rel.name,
                        "label": rel.description or rel.name,
                        "source": f"{src.name}:{srow.get(s_pk)}",
                        "target": f"{tgt.name}:{trow.get(t_pk)}",
                        "sourceType": src.name,
                        "targetType": tgt.name,
                    })

    return individuals, links


def _build_instance_layer_from_links(
    db: Session, entities: list[OntologyEntity], store: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """以连线为骨架构建实例层。

    按"每类取前 N 行"采样的话，连线两端几乎不可能同时落在样本里——实测
    59k 个实例里画出来的边只有 189 条。所以这里反过来：先看边，把**有连接
    的实例**优先放进图（按度数从高到低，让枢纽节点进中心），预算还有剩
    再补孤立节点。

    这样 twin 视图画出来的是真实的关联网络，而不是一片散点。
    """
    entity_map = {e.id: e for e in entities}
    all_links = store.links_by_type_pair()

    # 按关系分组。全局按度数取节点会让"边多的关系"吃光预算，另外几条关系
    # 一条边都画不出来（实测 5 条关系只显示了 2 条），所以每个关系分一份配额。
    by_rel: dict[str, list[tuple[tuple[str, str], tuple[str, str]]]] = {}
    for lk in all_links:
        a = (lk["fromType"], lk["fromKey"])
        b = (lk["toType"], lk["toKey"])
        by_rel.setdefault(lk["relId"], []).append((a, b))

    node_budget = _ABOX_MAX_INDIVIDUALS
    connected_budget = int(node_budget * 0.8)

    # 80% 预算给有连接的节点，每个关系均分
    chosen: list[tuple[str, str]] = []
    chosen_set: set[tuple[str, str]] = set()
    if by_rel:
        per_rel = max(1, connected_budget // len(by_rel))
        for rel_id, pairs in by_rel.items():
            degree: dict[tuple[str, str], int] = {}
            for a, b in pairs:
                degree[a] = degree.get(a, 0) + 1
                degree[b] = degree.get(b, 0) + 1
            taken = 0
            for key, _ in sorted(degree.items(), key=lambda kv: (-kv[1], kv[0])):
                if key in chosen_set:
                    continue
                chosen.append(key)
                chosen_set.add(key)
                taken += 1
                if taken >= per_rel or len(chosen) >= connected_budget:
                    break

    # 补孤立节点：各类型按主键顺序取，跳过已在图里的
    if len(chosen) < node_budget:
        per_type = max(1, (node_budget - len(chosen)) // max(1, len(entities)))
        for e in entities:
            rows = store.query(e.id, page=1, page_size=per_type, include_props=False)
            for item in rows.get("items", []):
                key = (e.id, item["objectKey"])
                if key in chosen_set:
                    continue
                chosen.append(key)
                chosen_set.add(key)
                if len(chosen) >= node_budget:
                    break
            if len(chosen) >= node_budget:
                break

    # 一次取回所有节点属性
    props_map = store.objects_by_keys(chosen)

    individuals: list[dict[str, Any]] = []
    for etype, okey in chosen:
        info = props_map.get((etype, okey))
        if info is None:
            continue                   # 对象已被删除，跳过
        e = entity_map.get(etype)
        individuals.append({
            "id": f"{etype}:{okey}",
            "objectType": etype,
            "objectTypeDisplay": e.name_cn if e else etype,
            "title": info.get("title") or okey,
            "primaryKeyValue": okey,
            "properties": {
                k: (None if v is None else str(v))
                for k, v in (info.get("properties") or {}).items()
            },
        })

    node_ids = {i["id"] for i in individuals}

    # 边也按关系配额，避免边多的关系把图堆满
    links: list[dict[str, Any]] = []
    per_rel_links = max(1, _ABOX_MAX_LINKS // max(1, len(by_rel)))
    for rel_id, pairs in by_rel.items():
        taken = 0
        for a, b in pairs:
            if taken >= per_rel_links or len(links) >= _ABOX_MAX_LINKS:
                break
            src_id = f"{a[0]}:{a[1]}"
            tgt_id = f"{b[0]}:{b[1]}"
            if src_id not in node_ids or tgt_id not in node_ids:
                continue
            links.append({
                "id": f"{rel_id}:{a[1]}:{b[1]}",
                "linkType": rel_id,
                "label": rel_id,
                "source": src_id,
                "target": tgt_id,
                "sourceType": a[0],
                "targetType": b[0],
            })
            taken += 1

    # 关系 id → 短名，供前端图例
    from app.services.object_link_service import definitions as _link_defs

    rel_meta = {r["id"]: r for r in _link_defs(db)}
    for lk in links:
        meta = rel_meta.get(lk["linkType"]) or {}
        lk["linkType"] = meta.get("name") or lk["linkType"]
        lk["label"] = meta.get("displayName") or lk["linkType"]

    return individuals, links


def _instance_layer(db: Session, entities: list[OntologyEntity]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """带 TTL 缓存的实例层入口。

    abox 和 instance-neighbors 都会调用；每次真实计算要对源库发 9 条查询，
    前端一刷新就可能连打多次，直接超 rate_limit_qps 而丢数据。缓存 15 秒。
    """
    global _abox_cache
    now = time.time()
    if _abox_cache is not None and now - _abox_cache[0] < _ABOX_CACHE_TTL_SECONDS:
        return _abox_cache[1], _abox_cache[2]

    # 有对象存储时走"以连线为骨架"的路径——否则按行采样会画出几乎为空的图
    store = get_object_store()
    if store is not None:
        individuals, links = _build_instance_layer_from_links(db, entities, store)
    else:
        individuals, links = _build_instance_layer(db, entities)

    _abox_cache = (now, individuals, links)
    return individuals, links
