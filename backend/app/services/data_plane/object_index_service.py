"""对象索引服务 —— 把对象实例物化到元数据库，并提供对象集合查询。

典型用法：
    svc = ObjectIndexService(db)
    svc.sync_all()                       # 全量重建索引
    rows = svc.rows_by_type()            # {object_type_id: [props...]} 供实例层使用
    page = svc.query("Customer", filter={...}, search="张", page=1)

设计要点：
    - 索引是**可再生副本**，源表永远是唯一真相；索引坏了重新 sync 即可。
    - 同步按对象类型整体替换（delete + insert），不做增量，优先保证结果正确。
    - 单类型行数超过 `MAX_ROWS_PER_TYPE` 时截断，并在 sync_state 里标记 truncated，
      避免把大表一次性塞进 SQLite。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_
from sqlalchemy.orm import Session

from app.models.entity import EntityAttribute, OntologyEntity
from app.models.object_index import ObjectIndexEntry, ObjectIndexSyncState
from app.services.object_store import get_object_store

logger = logging.getLogger(__name__)

# 单个对象类型最多索引多少行。换到 Postgres 后这个上限不再是存储压力，
# 而是"要不要为了一个对象类型等一次长同步"的取舍；真正的尺寸收窄靠 slice_expr。
MAX_ROWS_PER_TYPE = 5000
# 源库单次查询的 LIMIT（比索引上限稍大，便于发现"被截断"）
SOURCE_FETCH_LIMIT = MAX_ROWS_PER_TYPE + 1

# 每批写入多少行后提交（单事务过大时 SQLite 会报 unable to open database file）
_BATCH_SIZE = 500

# 同步互斥：手动触发（API）与定时触发（后台）共用。
# 同一时刻跑两轮会对着源库连打两倍查询，容易撞上 rate_limit_qps。
_SYNC_LOCK = threading.Lock()


# ── 主键 / 标题推断（与 studio 的 tbox 输出保持一致）──────────────

def primary_keys_of(entity: OntologyEntity) -> list[str]:
    """对象主键列（支持复合键）。

    真实业务表的粒度常常不是单列：例如 `dwd_crm_contract_vehicle_di` 是
    「合同 × 产品组合」，`contract_code` 会重复，必须用
    (contract_code, product_portfolio_id) 才能唯一定位一个实例。
    复合键在 `config_json.primary_key` 里写成列表。
    """
    cfg = entity.config_json or {}
    if isinstance(cfg, dict) and cfg.get("primary_key"):
        pk = cfg["primary_key"]
        return [str(pk)] if isinstance(pk, str) else [str(x) for x in pk]
    for a in entity.attributes:
        if a.required and a.name.endswith("_id"):
            return [a.name]
    if entity.attributes:
        return [entity.attributes[0].name]
    return ["id"]


def primary_key_of(entity: OntologyEntity) -> str:
    """单列主键（兼容旧调用点，返回复合键的第一列）。"""
    return primary_keys_of(entity)[0]


def object_key_of(entity: OntologyEntity, row: dict[str, Any]) -> str | None:
    """按主键列拼出实例唯一键；任一主键列为空则返回 None。

    复合键用 "|" 连接，例如 "231101398181817|1092"。
    """
    parts: list[str] = []
    for k in primary_keys_of(entity):
        v = row.get(k)
        if v is None:
            return None
        parts.append(str(v))
    return "|".join(parts)


def title_property_of(entity: OntologyEntity) -> str:
    for a in entity.attributes:
        low = a.name.lower()
        if "name" in low or "title" in low:
            return a.name
    return primary_key_of(entity)


def _stringify(value: Any) -> str | None:
    """属性一律字符串化：与 /studio/abox 既有输出一致，前端不必处理类型差异。"""
    if value is None:
        return None
    return str(value)


class ObjectIndexService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── 源库取数 ────────────────────────────────────────────
    def _fetch_source_rows(
        self, asset: Any, limit: int, filter_expr: str | None = None,
        source_sql: str | None = None,
    ) -> list[dict[str, Any]]:
        """从源表拉行。

        `filter_expr` 用于把大表收窄到有意义的切片（例如"只取最新快照日"）。
        真实数仓表动辄上百万行，把整表当对象集合没有意义，SQLite 索引也装不下。

        `source_sql` 用于**实体层**：实体不是某张表的一行，而是从多张事实表
        里 DISTINCT 出来的主数据（例如物料要从三张库龄表取并集）。这时用调用方
        给的 SELECT 代替 `SELECT * FROM <table>`，外层再包一层保证 LIMIT 语义，
        调用方的 SQL 就不必自己写 LIMIT。

        标识符引号跟随连接方言（MySQL 反引号 / PG 双引号），
        写死反引号会让 PG 的 AST 安全审查 ParseError(ast_failed)。
        """
        from app.connectors import ConnectorRegistry
        from app.models.connection import Connection
        from app.services.data_plane.execute_service import ExecuteRequest, ExecuteService

        table = (asset.locator or {}).get("table") or ""

        if source_sql and source_sql.strip():
            # 实体 SQL 自带 DISTINCT/UNION，filter_expr 对它没有意义，忽略
            inner = source_sql.strip().rstrip(";")
            sql = f"SELECT * FROM ({inner}) _entity_rows LIMIT {limit}"
        else:
            if not table:
                return []
            conn = self.db.get(Connection, asset.connection_id) if asset.connection_id else None
            quote = ConnectorRegistry.get(conn.type).quote_identifier() if conn else '"'
            sql = f"SELECT * FROM {quote}{table}{quote}"
            if filter_expr:
                sql += f" WHERE {filter_expr}"
            sql += f" LIMIT {limit}"

        result = ExecuteService(self.db).execute(
            ExecuteRequest(asset_id=asset.id, sql=sql, purpose="object_index.sync")
        )
        columns = result.columns or []
        return [dict(zip(columns, row)) for row in (result.rows or [])]

    # ── 同步 ────────────────────────────────────────────────
    def sync_object_type(self, entity: OntologyEntity, asset: Any) -> ObjectIndexSyncState:
        """整体重建一个对象类型的索引。"""
        state = self.db.get(ObjectIndexSyncState, entity.id)
        if state is None:
            state = ObjectIndexSyncState(object_type_id=entity.id)
            self.db.add(state)
            self.db.flush()

        state.source_table = (asset.locator or {}).get("table")
        state.synced_at = datetime.utcnow()

        # 每个对象类型可以单独收紧。两个过滤条件语义不同，不能混：
        #   filter_expr —— 口径对齐，必须和 cube 的 WHERE 一致（由 cube_filter_sync 维护）
        #   slice_expr  —— 尺寸收窄，为的是能塞进索引（如"只取最新快照日"），人为设定
        cfg = entity.config_json or {}
        filter_expr = cfg.get("filter_expr") if isinstance(cfg, dict) else None
        slice_expr = cfg.get("slice_expr") if isinstance(cfg, dict) else None
        where_parts = [f"({p})" for p in (filter_expr, slice_expr) if p]
        combined_where = " AND ".join(where_parts) or None
        max_rows = MAX_ROWS_PER_TYPE
        if isinstance(cfg, dict) and cfg.get("index_max_rows"):
            try:
                max_rows = max(1, int(cfg["index_max_rows"]))
            except (TypeError, ValueError):
                max_rows = MAX_ROWS_PER_TYPE

        try:
            rows = self._fetch_source_rows(
                asset, max_rows + 1, combined_where, cfg.get("source_sql")
                if isinstance(cfg, dict) else None,
            )
        except Exception as e:
            logger.warning("object_index: 同步 %s 失败: %s", entity.name, e)
            state.status = "error"
            state.error = str(e)[:500]
            state.row_count = 0
            self.db.commit()
            return state

        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]

        pk = primary_key_of(entity)
        title_prop = title_property_of(entity)

        # 只索引"建模过的属性"（属性名 ∪ 其物理列 ∪ 主键列），不存整行。
        # 真实数仓表动辄 150+ 列，整行物化会让 props_json 膨胀到 ~10KB/行，
        # 5000 行就是 50MB 的单条 INSERT，SQLite 会直接报 unable to open database file。
        keep = {a.source_field or a.name for a in entity.attributes}
        keep |= set(primary_keys_of(entity))
        keep.add(title_prop)

        # 优先写 Postgres 对象存储；没配 OBJECT_STORE_URL 时退回 SQLite 路径。
        # 两条路径的语义相同（整类重建），区别只在容量和查询能力。
        store = get_object_store()
        use_store = store is not None

        inserted = 0
        dup_keys = 0
        seen_keys: set[str] = set()
        store_payload: list[dict[str, Any]] = []
        sqlite_payload: list[dict[str, Any]] = []
        for row in rows:
            key = object_key_of(entity, row)
            if key is None:
                # 主键列为空的行无法构造对象身份，跳过
                continue
            if key in seen_keys:
                # 主键配置不足以唯一标识一行（粒度理解有误），跳过重复以免整批失败
                dup_keys += 1
                continue
            seen_keys.add(key)
            title = str(row.get(title_prop) or key)
            props = {k: _stringify(v) for k, v in row.items() if k in keep}
            if use_store:
                store_payload.append({
                    "__object_key__": key,
                    "__title__": title,
                    **props,
                })
            else:
                sqlite_payload.append({
                    "object_type_id": entity.id,
                    "object_key": key,
                    "title": title,
                    "props_json": props,
                    "source_table": state.source_table,
                    "synced_at": datetime.utcnow(),
                })
            inserted += 1

        try:
            if use_store:
                # 一个事务里 upsert + 清理消失行，失败则原数据不动
                store.replace_type(entity.id, store_payload, state.source_table)
                # 清掉 SQLite 里的旧副本，避免两份索引不一致
                self.db.execute(
                    delete(ObjectIndexEntry).where(
                        ObjectIndexEntry.object_type_id == entity.id
                    )
                )
                self.db.commit()
            else:
                # 整体替换：先删旧行，避免残留已消失的实例
                self.db.execute(
                    delete(ObjectIndexEntry).where(
                        ObjectIndexEntry.object_type_id == entity.id
                    )
                )
                # 用 Core 批量 insert + 分块提交，而不是逐条 ORM add。
                # 几万个 ORM 对象堆在 session 里会撑爆 identity map，且单事务太大时
                # SQLite 会报 "unable to open database file"。
                from sqlalchemy import insert

                for i in range(0, len(sqlite_payload), _BATCH_SIZE):
                    self.db.execute(
                        insert(ObjectIndexEntry), sqlite_payload[i:i + _BATCH_SIZE]
                    )
                    self.db.commit()
        except Exception as exc:
            self.db.rollback()
            if not use_store:
                # SQLite 路径失败时清掉该类型可能已提交的半批数据
                try:
                    self.db.execute(
                        delete(ObjectIndexEntry).where(
                            ObjectIndexEntry.object_type_id == entity.id
                        )
                    )
                    self.db.commit()
                except Exception:  # noqa: BLE001
                    self.db.rollback()
            logger.warning("object_index: 写入 %s 失败: %s", entity.name, exc)
            state.status = "error"
            state.error = str(exc)[:500]
            state.row_count = 0
            self.db.commit()
            return state

        if dup_keys:
            logger.warning(
                "object_index: %s 有 %s 行主键重复被跳过 —— primary_key 可能不足以唯一标识粒度",
                entity.name, dup_keys,
            )

        state.row_count = inserted
        state.status = "truncated" if truncated else ("ok" if inserted else "empty")
        state.error = None
        self.db.commit()
        return state

    def sync_all(self, *, skip_if_running: bool = False) -> dict[str, Any]:
        """按 ObjectBinding 把每个对象类型都同步一遍。

        `skip_if_running=True` 时，若已有同步在跑则立即返回 `{"busy": True}`（定时任务用）；
        否则会阻塞等待锁（手动触发用，保证用户拿到的是一次真实结果）。
        """
        acquired = _SYNC_LOCK.acquire(blocking=not skip_if_running)
        if not acquired:
            return {
                "busy": True,
                "syncedAt": datetime.utcnow().isoformat(),
                "total": 0, "ok": 0, "skipped": 0, "failed": 0, "results": [],
            }
        try:
            return self._sync_all_locked()
        finally:
            _SYNC_LOCK.release()

    def _sync_all_locked(self) -> dict[str, Any]:
        from app.models.asset import Asset
        from app.repositories.object_binding_repo import ObjectBindingRepository

        binding_repo = ObjectBindingRepository(self.db)
        assets_by_id = {
            a.id: a
            for a in self.db.query(Asset).filter(Asset.status == "active").all()
        }
        entities = self.db.query(OntologyEntity).all()

        results: list[dict[str, Any]] = []
        ok = skipped = failed = 0
        for entity in entities:
            binding = binding_repo.get_primary(entity.id)
            if not binding:
                results.append({
                    "objectType": entity.name, "status": "no_binding",
                    "rowCount": 0,
                })
                skipped += 1
                continue
            asset = assets_by_id.get(binding.asset_id)
            if not asset:
                results.append({
                    "objectType": entity.name, "status": "asset_missing",
                    "rowCount": 0,
                })
                skipped += 1
                continue

            state = self.sync_object_type(entity, asset)
            results.append({
                "objectType": entity.name,
                "status": state.status,
                "rowCount": state.row_count,
                "table": state.source_table,
                "error": state.error,
            })
            if state.status == "error":
                failed += 1
            else:
                ok += 1

        # 对象实例变了，连线必须跟着重算——否则会留下指向已消失实例的边。
        # 连线物化就是几条 SQL，比对象同步便宜得多。
        links: dict[str, Any] = {}
        try:
            from app.services.object_link_service import rebuild_all

            links = rebuild_all(self.db)
        except Exception as e:  # noqa: BLE001
            logger.warning("object_index: 连线重建失败: %s", e)
            links = {"ok": False, "message": str(e)[:300]}

        # 血缘边同理：绑定关系变了（比如新增实体层），图要跟着更新
        lineage: dict[str, Any] = {}
        try:
            from app.services.data_plane.field_lineage import build as build_fields
            from app.services.data_plane.lineage_builder import rebuild as rebuild_lineage

            lineage = {
                "config": rebuild_lineage(self.db),
                "fields": build_fields(self.db),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("object_index: 血缘重建失败: %s", e)
            lineage = {"ok": False, "message": str(e)[:300]}

        return {
            "syncedAt": datetime.utcnow().isoformat(),
            "total": len(entities),
            "ok": ok,
            "skipped": skipped,
            "failed": failed,
            "results": results,
            "links": links,
        }

    # ── 查询 ────────────────────────────────────────────────
    def rows_by_type(self, limit_per_type: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """全部索引行，按对象类型分组（供实例层构图使用）。

        `limit_per_type` 用于浏览类场景：索引可能有几万行（真实数仓表），
        一次全加载会让接口从几毫秒退化到两秒。展示层只需要前 N 个实例即可。
        """
        store = get_object_store()
        if store is not None:
            return store.rows_by_type(limit_per_type)

        out: dict[str, list[dict[str, Any]]] = {}
        if limit_per_type is None:
            for entry in self.db.query(ObjectIndexEntry).all():
                out.setdefault(entry.object_type_id, []).append(entry.props_json or {})
            return out

        # 按类型各取前 N 行：先查类型清单，再逐类型 limit 查询
        from sqlalchemy import func, select

        type_ids = [
            r[0] for r in
            self.db.execute(
                select(ObjectIndexEntry.object_type_id)
                .group_by(ObjectIndexEntry.object_type_id)
            ).all()
        ]
        for tid in type_ids:
            rows = (
                self.db.query(ObjectIndexEntry.props_json)
                .filter(ObjectIndexEntry.object_type_id == tid)
                .order_by(ObjectIndexEntry.object_key)
                .limit(limit_per_type)
                .all()
            )
            out[tid] = [dict(r[0] or {}) for r in rows]
        return out

    def count(self) -> int:
        store = get_object_store()
        if store is not None:
            return store.count()
        return self.db.query(func.count(ObjectIndexEntry.id)).scalar() or 0

    def get_props(self, object_type_id: str, object_key: str) -> dict[str, Any] | None:
        """取单个实例的属性快照；不存在返回 None。

        动作的前置条件在目标实例上求值，走索引比回源查表快且不受连接限流影响。
        """
        store = get_object_store()
        if store is not None:
            return store.get_props(object_type_id, object_key)
        entry = (
            self.db.query(ObjectIndexEntry)
            .filter(
                ObjectIndexEntry.object_type_id == object_type_id,
                ObjectIndexEntry.object_key == str(object_key),
            )
            .first()
        )
        return dict(entry.props_json or {}) if entry else None

    def has_data(self) -> bool:
        store = get_object_store()
        if store is not None:
            return store.has_data()
        return self.count() > 0

    def query(
        self,
        object_type_id: str,
        *,
        filter: dict[str, Any] | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
        include_props: bool = True,
        prefer_key: str | None = None,
    ) -> dict[str, Any]:
        """对象集合查询：等值过滤 + 标题模糊搜索 + 分页。

        `filter` 只支持等值匹配（索引表里属性是 JSON，范围查询需要方言相关的
        JSON 函数；等值已经覆盖了绝大多数对象筛选场景）。

        `prefer_key`：把该属性有值的实例排在前面。用于"按某属性过滤指标"的场景
        —— 没有该属性的实例点进去必然查不出指标（例如未清项里没有客户编码的行）。
        """
        page = max(1, page)
        page_size = max(1, min(page_size, 200))

        store = get_object_store()
        if store is not None:
            # Postgres：过滤/排序/分页全部下推到 SQL，不再把整表读进内存
            return store.query(
                object_type_id,
                filter=filter,
                search=search,
                page=page,
                page_size=page_size,
                include_props=include_props,
                prefer_key=prefer_key,
            )

        # 只允许合法标识符，避免拼进 JSON 路径时被注入
        prefer_key = prefer_key if (prefer_key and prefer_key.isidentifier()) else None

        q = self.db.query(ObjectIndexEntry).filter(
            ObjectIndexEntry.object_type_id == object_type_id
        )
        if search:
            like = f"%{search}%"
            q = q.filter(or_(
                ObjectIndexEntry.title.ilike(like),
                ObjectIndexEntry.object_key.ilike(like),
            ))

        if prefer_key:
            from sqlalchemy import case, func

            expr = func.json_extract(ObjectIndexEntry.props_json, f"$.{prefer_key}")
            # 空值/空串排到最后（0 优先）
            q = q.order_by(case((or_(expr.is_(None), expr == ""), 1), else_=0))
            q = q.order_by(ObjectIndexEntry.title, ObjectIndexEntry.object_key)
            rows = q.all()
        else:
            rows = q.order_by(ObjectIndexEntry.title, ObjectIndexEntry.object_key).all()

        # 等值过滤在 Python 侧做：SQLite 的 JSON 查询语法与 PG 不一致，
        # 用统一逻辑避免方言分叉。索引规模受 MAX_ROWS_PER_TYPE 约束。
        if filter:
            pairs = [(str(k), str(v)) for k, v in filter.items() if v is not None]
            rows = [
                r for r in rows
                if all((r.props_json or {}).get(k) == v for k, v in pairs)
            ]

        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]

        items = []
        for r in page_rows:
            item = {
                "id": f"{object_type_id}:{r.object_key}",
                "objectType": object_type_id,
                "objectKey": r.object_key,
                "title": r.title,
            }
            if include_props:
                item["properties"] = r.props_json or {}
            items.append(item)

        return {
            "objectType": object_type_id,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "items": items,
        }

    def stats(self) -> dict[str, Any]:
        states = self.db.query(ObjectIndexSyncState).all()
        total_rows = self.count()
        return {
            "totalRows": total_rows,
            "objectTypes": [
                {
                    "objectTypeId": s.object_type_id,
                    "status": s.status,
                    "rowCount": s.row_count,
                    "table": s.source_table,
                    "syncedAt": s.synced_at.isoformat() if s.synced_at else None,
                    "error": s.error,
                }
                for s in states
            ],
        }
