"""LineageService — 本体侧血缘。

血缘范围：Asset → ObjectType → Action（不画上游 Connection→Asset，不画上游 ETL）。
事件订阅：
- binding.created/updated → upsert (asset → object_type, binds_to)
- binding.deleted → 标 weight=0
- execution.completed → 按 purpose 白名单写 ad-hoc 边
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.connection import Connection
from app.models.entity import OntologyEntity
from app.models.lineage_edge import LineageEdge
from app.models.rule import EntityAction
from app.repositories.lineage_edge_repo import LineageEdgeRepository

logger = logging.getLogger(__name__)


# 仅这些 purpose 前缀会让 execution.completed 写血缘 ad-hoc 边。
# 避免 probe.* 等"治理动作"也产生大量节点。
LINEAGE_PURPOSE_PREFIXES = ("mnp.", "scene.", "scenes.", "action.write", "ontology.", "copilot.")


class LineageService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = LineageEdgeRepository(db)

    # ── 写边 ───────────────────────────────────────────
    def upsert_binding(self, asset_id: str, object_type_id: str, *, via_module: str = "binding") -> None:
        self.repo.upsert(
            source_kind="asset", source_id=asset_id,
            target_kind="object_type", target_id=object_type_id,
            relation="binds_to", via_module=via_module,
        )
        self.db.commit()

    def deprecate_binding(self, asset_id: str, object_type_id: str) -> None:
        self.repo.deprecate_for_binding(object_type_id, asset_id)
        self.db.commit()

    def upsert_execution(self, asset_id: str, purpose: str) -> None:
        if not asset_id or not purpose:
            return
        if not any(purpose.startswith(p) for p in LINEAGE_PURPOSE_PREFIXES):
            return
        # 解析 purpose 形式为 "<domain>.<feature>" → object_type_id 留空，
        # 这里写 source=asset, target=object_type(by purpose 标签)，
        # 只在 binding 已存在时强化 weight；其余通过 ObjectBinding 写入主路径。
        # 简化：仅累加 ExecutionLog 已有 binds_to 边的 weight（找该 asset 的所有 binding）。
        from app.repositories.object_binding_repo import ObjectBindingRepository
        bindings = ObjectBindingRepository(self.db).list_for_asset(asset_id)
        for b in bindings:
            self.repo.upsert(
                source_kind="asset", source_id=asset_id,
                target_kind="object_type", target_id=b.object_type_id,
                relation="reads", via_module="execute", via_purpose=purpose,
            )
        self.db.commit()

    # ── 查询 ───────────────────────────────────────────
    def get_for_asset(self, asset_id: str, depth: int = 2) -> dict:
        return self._collect("asset", asset_id, depth)

    def get_for_object_type(self, ot_id: str, depth: int = 2) -> dict:
        return self._collect("object_type", ot_id, depth)

    def get_overview(self, detail: str = "coarse") -> dict:
        """全局血缘：所有非废弃边 + 涉及节点的元数据。

        `detail`
            ``coarse``（默认）只给"资产 → 对象类型 → 动作/规则"这一层，
                      十几对节点，图能看清。
            ``full``  额外带上字段级：指标 → 物理列 → 对象属性。节点会到几百个，
                      用来看"这个指标的原始列在哪"，不适合当默认首屏。
        """
        coarse_kinds = {"asset", "object_type", "action", "rule"}
        q = self.db.query(LineageEdge).filter(LineageEdge.weight > 0)
        edges = q.all()
        if detail != "full":
            edges = [
                e for e in edges
                if e.source_kind in coarse_kinds and e.target_kind in coarse_kinds
            ]

        node_keys: set[tuple[str, str]] = set()
        for e in edges:
            node_keys.add((e.source_kind, e.source_id))
            node_keys.add((e.target_kind, e.target_id))
        node_payload = self._enrich_nodes(node_keys)
        edge_payload = [self._edge_dict(e) for e in edges]
        return {"nodes": node_payload, "edges": edge_payload}

    def _collect(self, kind: str, node_id: str, depth: int) -> dict:
        edges, nodes = self.repo.find_neighbors(kind, node_id, depth=depth)
        node_payload = self._enrich_nodes(nodes)
        edge_payload = [self._edge_dict(e) for e in edges]
        return {"nodes": node_payload, "edges": edge_payload}

    @staticmethod
    def _edge_dict(e: LineageEdge) -> dict:
        return {
            "source": {"kind": e.source_kind, "id": e.source_id},
            "target": {"kind": e.target_kind, "id": e.target_id},
            "relation": e.relation,
            "via_module": e.via_module,
            "via_purpose": e.via_purpose,
            "weight": e.weight,
        }

    def _enrich_nodes(self, node_keys) -> list[dict]:
        asset_ids = [i for k, i in node_keys if k == "asset"]
        ot_ids = [i for k, i in node_keys if k == "object_type"]
        action_ids = [i for k, i in node_keys if k == "action"]

        assets: dict[str, Asset] = {}
        conns: dict[str, Connection] = {}
        if asset_ids:
            asset_rows = self.db.query(Asset).filter(Asset.id.in_(asset_ids)).all()
            assets = {a.id: a for a in asset_rows}
            conn_ids = {a.connection_id for a in asset_rows if a.connection_id}
            if conn_ids:
                conns = {
                    c.id: c for c in
                    self.db.query(Connection).filter(Connection.id.in_(conn_ids)).all()
                }

        ots: dict[str, OntologyEntity] = {}
        if ot_ids:
            ots = {
                o.id: o for o in
                self.db.query(OntologyEntity).filter(OntologyEntity.id.in_(ot_ids)).all()
            }

        actions: dict[str, EntityAction] = {}
        if action_ids:
            actions = {
                a.id: a for a in
                self.db.query(EntityAction).filter(EntityAction.id.in_(action_ids)).all()
            }

        out: list[dict] = []
        # 字段级节点的中文标题来自 cube 模型；取不到就退回 id，至少不空白
        metric_titles: dict[str, str] = {}
        if any(k in ("metric", "column", "attribute") for k, _ in node_keys):
            try:
                from app.services.data_plane.field_lineage import metric_titles as _mt

                metric_titles = _mt()
            except Exception:  # noqa: BLE001
                metric_titles = {}

        for kind, ident in sorted(node_keys):
            label: str | None = None
            sub_label: str | None = None
            extra: dict = {}
            if kind == "metric":
                # ident 形如 finance_summary.total_assets
                cube = ident.split(".", 1)[0] if "." in ident else ""
                label = metric_titles.get(ident) or ident
                sub_label = cube or "指标"
                extra = {"cube": cube, "member": ident}
            elif kind == "column":
                # ident 形如 dws_cdep_financial_indicator_month.total_assets
                tbl, _, col = ident.rpartition(".")
                label = col or ident
                sub_label = tbl or "列"
                extra = {"table": tbl, "column": col or ident}
            elif kind == "attribute":
                # ident 形如 FinanceMonth.total_assets
                etype, _, aname = ident.rpartition(".")
                label = aname or ident
                sub_label = etype or "属性"
                extra = {"objectType": etype, "name": aname or ident}
            elif kind == "asset":
                a = assets.get(ident)
                if a:
                    label = a.alias or a.name
                    sub_label = a.kind
                    extra = {
                        "name": a.name,
                        "alias": a.alias,
                        "kind": a.kind,
                        "connection_id": a.connection_id,
                        "connection_name": (conns.get(a.connection_id).name if a.connection_id and conns.get(a.connection_id) else None),
                        "status": a.status,
                    }
            elif kind == "object_type":
                o = ots.get(ident)
                if o:
                    label = o.name_cn or o.name
                    sub_label = o.name
                    extra = {"name": o.name, "name_cn": o.name_cn, "tier": o.tier}
            elif kind == "action":
                act = actions.get(ident)
                if act:
                    label = act.name
                    # EntityAction 没有 `type` 字段：分类是 `action_type`
                    # （notification / call_function / sql_exec / api_call ...）。
                    # 老代码写的是 `act.type`，在 entity_actions 为空时从不执行，
                    # 一旦有动作就 AttributeError → 血缘接口整体 500。
                    sub_label = act.action_type
                    extra = {
                        "name": act.name,
                        "type": act.action_type,
                        "entity_id": act.entity_id,
                        "category": act.category,
                    }
            out.append({
                "kind": kind,
                "id": ident,
                "label": label,
                "sub_label": sub_label,
                "extra": extra or None,
            })
        return out
