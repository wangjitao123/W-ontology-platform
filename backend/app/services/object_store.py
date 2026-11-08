"""对象存储 —— 把对象索引从 SQLite 换成 Postgres。

## 为什么换

旧实现把对象实例和业务数据放在同一个 SQLite 文件里，有三处硬伤：

1. **分页在 Python 里做。** ``query()`` 先把满足条件的**所有**行读进内存，
   再切片。59,076 行时还能忍，到几十万行就是每次查询都全表加载。
2. **同步只能整表删插。** 每次 ``DELETE`` 整个类型再分批 ``INSERT``，
   期间读到的是空集；大类型同步要好几秒。
3. **塞不下就只能抽样。** ``WmsAging`` 源表 179 万行，靠"只取最新快照日"
   压到 1.3 万行；``ArOpenItem`` 948 万行压到 607 行。**那是抽样，不是索引。**

换成 Postgres 后：JSONB 上的 GIN 索引让"按任意属性等值筛"走索引；
排序/分页下推到 SQL；增量 upsert 配合 ``synced_at`` 水位线清掉消失的行。

## 设计要点

- **主键是 (object_type_id, object_key)**，与旧表一致。对象键本身可能是
  复合值（如 ``合同号|产品组合``），由上层拼好，这里当不透明字符串。
- **props 存 JSONB**，``jsonb_path_ops`` 的 GIN 索引体积小、只支持 ``@>``，
  而这正好是等值筛选需要的算子。
- **同步是"upsert + 水位线删除"**：本批全部写 ``synced_at = batch``，
  再删掉 ``synced_at < batch`` 的旧行。这样增量同步天然幂等，
  源表删掉的行也会跟着消失。
- **不可用时优雅退化**：``OBJECT_STORE_URL`` 为空则返回 None，
  调用方退回原来的 SQLite 路径。换存储不该变成一个硬性迁移。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 建表 + 索引。全部 IF NOT EXISTS，可重复执行。
_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS object_index (
        object_type_id text        NOT NULL,
        object_key     text        NOT NULL,
        title          text        NOT NULL DEFAULT '',
        props          jsonb       NOT NULL DEFAULT '{}'::jsonb,
        source_table   text,
        synced_at      timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (object_type_id, object_key)
    )
    """,
    # 列表页按 (title, object_key) 排序，主键前缀已含 object_type_id，
    # 这条覆盖索引让分页不必回表读 title。
    """
    CREATE INDEX IF NOT EXISTS ix_object_index_title
        ON object_index (object_type_id, title, object_key)
    """,
    # 等值筛选走这条。jsonb_path_ops 比默认 jsonb_ops 小很多。
    """
    CREATE INDEX IF NOT EXISTS ix_object_index_props
        ON object_index USING gin (props jsonb_path_ops)
    """,
    # 物化的实例级连线。一条关系一行，方向由 (from_type, from_key) → (to_type, to_key)
    # 表达；查询"这个对象连了谁"时两个方向都查（A→B 存一次，B→A 靠索引反查）。
    """
    CREATE TABLE IF NOT EXISTS object_link (
        rel_id     text        NOT NULL,
        from_type  text        NOT NULL,
        from_key   text        NOT NULL,
        to_type    text        NOT NULL,
        to_key     text        NOT NULL,
        built_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (rel_id, from_key, to_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_object_link_from
        ON object_link (from_type, from_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_object_link_to
        ON object_link (to_type, to_key)
    """,
)

# 允许在 join_keys 里声明的一元变换。值是白名单，拼进 SQL 前必校验。
_TRANSFORMS = {
    "month": "left({expr}, 7)",
    "upper": "upper({expr})",
    "lower": "lower({expr})",
    "trim": "btrim({expr})",
}

_UPSERT_SQL = """
    INSERT INTO object_index (object_type_id, object_key, title, props, source_table, synced_at)
    VALUES %s
    ON CONFLICT (object_type_id, object_key) DO UPDATE SET
        title        = EXCLUDED.title,
        props        = EXCLUDED.props,
        source_table = EXCLUDED.source_table,
        synced_at    = EXCLUDED.synced_at
"""

_BATCH = 1000


def _prop_expr(alias: str, column: str, transform: str | None = None) -> str:
    """把属性列变成 SQL 表达式。

    ``column`` 校验为标识符后才内插（psycopg2 不能把 JSON 键当参数绑）；
    ``transform`` 只认白名单里的几个，其余忽略。
    """
    if not (column and column.isidentifier()):
        raise ValueError(f"非法的 join 列名: {column!r}")
    expr = f"{alias}.props ->> '{column}'"
    t = (transform or "").strip().lower()
    if not t:
        return expr
    tpl = _TRANSFORMS.get(t)
    if tpl is None:
        return expr          # 未知变换当没写，不猜
    return tpl.format(expr=expr)


class PostgresObjectStore:
    """对象索引的 Postgres 后端。线程安全（连接池），可并发读写。"""

    def __init__(self, url: str, *, pool_size: int = 5, max_overflow: int = 10):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import QueuePool

        self.url = url
        self._engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_timeout=10,
            future=True,
        )
        self._ensure_schema()

    # ── 建表 ──────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        from sqlalchemy import text

        with self._engine.begin() as conn:
            for stmt in _SCHEMA_SQL:
                conn.execute(text(stmt))
        logger.info("对象存储: Postgres schema 就绪 (%s)", self._safe_url())

    def _safe_url(self) -> str:
        """日志里不带密码。"""
        url = self.url
        if "@" in url and "//" in url:
            head, tail = url.split("//", 1)
            creds, host = tail.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{head}//{user}:***@{host}"
        return url

    # ── 写入 ──────────────────────────────────────────────

    def replace_type(
        self,
        object_type_id: str,
        rows: Iterable[dict[str, Any]],
        source_table: str | None = None,
    ) -> int:
        """用一批行覆盖某个对象类型（增量 upsert + 水位线删除）。

        `rows` 是属性字典，必须含 `__object_key__` 和 `__title__`
        （由 ObjectIndexService 组装），其余键原样进 props。
        """
        batch = datetime.now(timezone.utc)
        values: list[tuple] = []
        for row in rows:
            key = row.get("__object_key__")
            if key is None:
                continue          # 主键缺值 = 没有对象身份，跳过
            props = {k: v for k, v in row.items() if not k.startswith("__")}
            values.append((
                object_type_id,
                str(key),
                str(row.get("__title__") or ""),
                json.dumps(props, ensure_ascii=False, default=str),
                source_table,
                batch,
            ))

        from sqlalchemy import text
        from psycopg2.extras import execute_values

        raw = self._engine.raw_connection()
        try:
            cur = raw.cursor()
            for i in range(0, len(values), _BATCH):
                execute_values(cur, _UPSERT_SQL, values[i:i + _BATCH], page_size=_BATCH)
            # 源表里已经不存在的行：本批没被写到的，synced_at 落后于 batch
            cur.execute(
                "DELETE FROM object_index "
                "WHERE object_type_id = %s AND synced_at < %s",
                (object_type_id, batch),
            )
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            raw.commit()
            cur.close()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()

        if removed:
            logger.info(
                "对象存储: %s 清理了 %d 行源表已消失的实例", object_type_id, removed,
            )
        return len(values)

    def delete_type(self, object_type_id: str) -> int:
        from sqlalchemy import text

        with self._engine.begin() as conn:
            r = conn.execute(
                text("DELETE FROM object_index WHERE object_type_id = :t"),
                {"t": object_type_id},
            )
            return r.rowcount or 0

    # ── 读取 ──────────────────────────────────────────────

    def count(self) -> int:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM object_index")).scalar() or 0)

    def count_by_type(self) -> dict[str, int]:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT object_type_id, count(*) FROM object_index GROUP BY 1")
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    def has_data(self) -> bool:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            return conn.execute(text("SELECT 1 FROM object_index LIMIT 1")).first() is not None

    def rows_by_type(self, limit_per_type: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """按类型分组返回属性，供实例层构图。`limit_per_type` 下推到 SQL。"""
        from sqlalchemy import text

        out: dict[str, list[dict[str, Any]]] = {}
        with self._engine.connect() as conn:
            if limit_per_type is None:
                rows = conn.execute(
                    text("SELECT object_type_id, props FROM object_index")
                ).all()
            else:
                # 每类取前 N：窗口函数一次查完，避免按类型循环发查询
                rows = conn.execute(
                    text(
                        "SELECT object_type_id, props FROM ("
                        "  SELECT object_type_id, props,"
                        "         row_number() OVER (PARTITION BY object_type_id"
                        "                            ORDER BY object_key) AS rn"
                        "  FROM object_index"
                        ") s WHERE rn <= :n"
                    ),
                    {"n": int(limit_per_type)},
                ).all()
        for tid, props in rows:
            out.setdefault(tid, []).append(dict(props or {}))
        return out

    def get_props(self, object_type_id: str, object_key: str) -> dict[str, Any] | None:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT props FROM object_index "
                    "WHERE object_type_id = :t AND object_key = :k"
                ),
                {"t": object_type_id, "k": str(object_key)},
            ).first()
        return dict(row[0]) if row and row[0] is not None else None

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
        """对象集合查询：等值过滤 + 标题模糊 + 分页，全部下推到 SQL。"""
        from sqlalchemy import text

        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        prefer_key = prefer_key if (prefer_key and prefer_key.isidentifier()) else None

        where = ["object_type_id = :tid"]
        params: dict[str, Any] = {"tid": object_type_id}

        # 空过滤值不参与筛选（前端常传空串）
        pairs = {str(k): v for k, v in (filter or {}).items() if v is not None and v != ""}
        if pairs:
            where.append("props @> cast(:filter as jsonb)")
            params["filter"] = json.dumps(pairs, ensure_ascii=False, default=str)

        if search:
            where.append("(title ILIKE :like OR object_key ILIKE :like)")
            params["like"] = f"%{search}%"

        where_sql = " AND ".join(where)

        # prefer_key 已校验为标识符，可安全内插（psycopg2 不能把列名当参数绑）。
        order = ""
        if prefer_key:
            order = (
                f"(props ->> '{prefer_key}' IS NULL "
                f"OR props ->> '{prefer_key}' = '') ASC, "
            )
        order += "title ASC, object_key ASC"

        sql_cols = "object_key, title" + (", props" if include_props else "")
        with self._engine.connect() as conn:
            total = int(conn.execute(
                text(f"SELECT count(*) FROM object_index WHERE {where_sql}"), params,
            ).scalar() or 0)
            rows = conn.execute(
                text(
                    f"SELECT {sql_cols} FROM object_index WHERE {where_sql} "
                    f"ORDER BY {order} LIMIT :lim OFFSET :off"
                ),
                {**params, "lim": page_size, "off": (page - 1) * page_size},
            ).all()

        items = []
        for r in rows:
            item = {
                "id": f"{object_type_id}:{r[0]}",
                "objectType": object_type_id,
                "objectKey": r[0],
                "title": r[1],
            }
            if include_props:
                item["properties"] = dict(r[2] or {})
            items.append(item)

        return {
            "objectType": object_type_id,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "items": items,
        }

    # ── 实例级连线（物化）──────────────────────────────────

    def rebuild_links(
        self,
        rel_id: str,
        from_type: str,
        to_type: str,
        join_pairs: list[dict[str, str]],
    ) -> int:
        """用一条 SQL 把某个关系的实例连线算出来并写入 object_link。

        ``join_pairs`` 来自 ``EntityRelation.join_keys``，每项形如
        ``{"from_column": "contract_id", "to_column": "contract_id"}``，
        可选 ``from_transform`` / ``to_transform``（见 ``_TRANSFORMS``）。

        为什么用 SQL 而不是 Python 双层循环：``WmsAging``(1.3 万) 与
        ``B2bAging``(2.9 万) 的两两比较是 3.7 亿次，内存里跑不动；
        Postgres 用哈希连接一遍就完事。
        """
        if not join_pairs:
            return 0

        conds = []
        for p in join_pairs:
            fc = p.get("from_column")
            tc = p.get("to_column")
            if not (fc and tc):
                return 0
            conds.append((
                _prop_expr("s", fc, p.get("from_transform")),
                _prop_expr("t", tc, p.get("to_transform")),
            ))
        on_sql = " AND ".join(f"{a} = {b}" for a, b in conds)

        from sqlalchemy import text

        batch = datetime.now(timezone.utc)
        sql = (
            "INSERT INTO object_link "
            "  (rel_id, from_type, from_key, to_type, to_key, built_at) "
            "SELECT :rel, :ft, s.object_key, :tt, t.object_key, :batch "
            "FROM (SELECT object_key, props FROM object_index "
            "      WHERE object_type_id = :ft) s "
            "JOIN (SELECT object_key, props FROM object_index "
            "      WHERE object_type_id = :tt) t "
            f"  ON {on_sql} "
            # 必须 DO UPDATE 刷新 built_at。用 DO NOTHING 的话旧行的 built_at
            # 保持上一批的时间，紧接着的水位线删除会把它当成陈旧数据清掉，
            # 结果第二次重建把边全删了（这个 bug 是 test_rebuild_is_idempotent
            # 抓出来的）。
            "ON CONFLICT (rel_id, from_key, to_key) "
            "  DO UPDATE SET built_at = EXCLUDED.built_at"
        )

        with self._engine.begin() as conn:
            conn.execute(text(sql), {
                "rel": rel_id, "ft": from_type, "tt": to_type, "batch": batch,
            })
            # 清掉本批没写到的旧边（源数据变了、原来能连的现在连不上）
            conn.execute(
                text(
                    "DELETE FROM object_link "
                    "WHERE rel_id = :rel AND built_at < :batch"
                ),
                {"rel": rel_id, "batch": batch},
            )
            n = conn.execute(
                text("SELECT count(*) FROM object_link WHERE rel_id = :rel"),
                {"rel": rel_id},
            ).scalar()
        return int(n or 0)

    def count_links(self) -> int:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM object_link")).scalar() or 0)

    def link_counts_by_rel(self) -> dict[str, int]:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT rel_id, count(*) FROM object_link GROUP BY 1")
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    def delete_links(self, rel_id: str) -> int:
        from sqlalchemy import text

        with self._engine.begin() as conn:
            return conn.execute(
                text("DELETE FROM object_link WHERE rel_id = :r"), {"r": rel_id},
            ).rowcount or 0

    def links_of_object(
        self,
        object_type_id: str,
        object_key: str,
        *,
        rel_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """双向取某个实例的邻居。

        存储只保留一个方向（from → to），这里把"作为源"和"作为目标"两种情况
        都查出来，并标明方向，调用方不用关心存的是哪一边。
        """
        from sqlalchemy import text

        limit = max(1, min(int(limit), 1000))
        params: dict[str, Any] = {"t": object_type_id, "k": str(object_key), "lim": limit}
        rel_filter = ""
        if rel_ids:
            rel_filter = " AND rel_id = ANY(:rels)"
            params["rels"] = list(rel_ids)

        sql = (
            "SELECT rel_id, from_type, from_key, to_type, to_key, 'out' AS dir "
            "FROM object_link WHERE from_type = :t AND from_key = :k" + rel_filter +
            " UNION ALL "
            "SELECT rel_id, from_type, from_key, to_type, to_key, 'in' AS dir "
            "FROM object_link WHERE to_type = :t AND to_key = :k" + rel_filter +
            " LIMIT :lim"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).all()

        out = []
        for rel_id, ftype, fkey, ttype, tkey, direction in rows:
            out.append({
                "relId": rel_id,
                "direction": direction,
                # 归一化成"对方是谁"，方便前端直接渲染
                "peerType": ttype if direction == "out" else ftype,
                "peerKey": tkey if direction == "out" else fkey,
                "fromType": ftype, "fromKey": fkey,
                "toType": ttype, "toKey": tkey,
            })
        return out

    def links_by_type_pair(self) -> list[dict[str, Any]]:
        """twin 视图用：返回所有连线（带类型与键），供前端画图。"""
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT rel_id, from_type, from_key, to_type, to_key "
                    "FROM object_link"
                )
            ).all()
        return [
            {"relId": r[0], "fromType": r[1], "fromKey": r[2],
             "toType": r[3], "toKey": r[4]}
            for r in rows
        ]

    def objects_by_keys(
        self, keys: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """批量取 {(type, key): {title, props}}，给"连线邻居"补标题用。

        邻居通常几十到几百个，逐条 get_props 也能跑，但一次 IN 更强。
        """
        from sqlalchemy import text

        if not keys:
            return {}
        out: dict[tuple[str, str], dict[str, Any]] = {}
        # 按类型分组，用 (type, ANY(keys)) 走主键索引
        by_type: dict[str, list[str]] = {}
        for t, k in keys:
            by_type.setdefault(t, []).append(k)

        with self._engine.connect() as conn:
            for t, ks in by_type.items():
                rows = conn.execute(
                    text(
                        "SELECT object_key, title, props FROM object_index "
                        "WHERE object_type_id = :t AND object_key = ANY(:ks)"
                    ),
                    {"t": t, "ks": ks},
                ).all()
                for key, title, props in rows:
                    out[(t, key)] = {
                        "objectKey": key,
                        "title": title,
                        "properties": dict(props or {}),
                    }
        return out

    def health(self) -> dict[str, Any]:
        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"ok": True, "url": self._safe_url(), "rows": self.count()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "url": self._safe_url(), "message": str(e)}


# 进程内单例：连接池要复用，不能每个请求新建 engine
_STORE: PostgresObjectStore | None = None
_STORE_URL: str | None = None


def get_object_store() -> PostgresObjectStore | None:
    """返回对象存储；未配置 URL 时返回 None（调用方退回 SQLite）。"""
    global _STORE, _STORE_URL

    from app.config import settings

    url = (settings.OBJECT_STORE_URL or "").strip()
    if not url:
        return None
    if _STORE is not None and _STORE_URL == url:
        return _STORE
    try:
        _STORE = PostgresObjectStore(url)
        _STORE_URL = url
        return _STORE
    except Exception as e:  # noqa: BLE001
        # 配置错了不应该让整个应用起不来：退回 SQLite 并留一条显眼的日志
        logger.error("对象存储: 初始化失败，退回 SQLite 索引: %s", e)
        _STORE = None
        _STORE_URL = url
        return None


def reset_object_store() -> None:
    """测试用：清掉单例。"""
    global _STORE, _STORE_URL
    if _STORE is not None:
        _STORE._engine.dispose()
    _STORE = None
    _STORE_URL = None
