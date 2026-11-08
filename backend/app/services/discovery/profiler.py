"""Doris 元数据扫描：把 165 张表读成可推导的结构。

## 为什么要自己扫，不用现成的

`ai_ontology_builder.py` 是**对话式 LLM 建模**（而且是联通领域写死的 prompt），
它不会去看数仓里实际有什么表。所以"从表结构自动发现对象类型"这件事，
仓库里没有现成的实现。

## 只读 information_schema

不 `SELECT *` 任何业务表：165 张表、4246 列，全扫一遍既慢又不必要。
对象类型和属性完全可以从"表名 + 列名 + 类型"推出来——列名本身已经编码了
语义（`material_code` / `material_name` 摆明了是物料）。

只有需要判断"这个 key 是不是真的唯一"时才抽样（`sample_keys()`），
那一步也是带 LIMIT 的，不会扫全表。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 目标 schema 及其在数仓里的角色
LAYERS = {
    "dwh_dwd": "dwd",   # 明细
    "dwh_dws": "dws",   # 汇总
    "dwh_ads": "ads",   # 应用层
}

# 列名后缀 → 语义角色
_ROLE_SUFFIXES: list[tuple[str, str]] = [
    ("_code", "key"),
    ("_id", "key"),
    ("_no", "key"),
    ("_key", "key"),
    ("_name", "label"),
    ("_desc", "label"),
    ("_title", "label"),
    ("_type", "category"),
    ("_type_str", "category"),
    ("_class", "category"),
    ("_category", "category"),
    ("_group", "category"),
    ("_state", "status"),
    ("_state_str", "status"),
    ("_status", "status"),
    ("_flag", "status"),
    ("_deleted", "status"),
    ("_date", "datetime"),
    ("_time", "datetime"),
    ("_month", "datetime"),
    ("_day", "datetime"),
    ("_qty", "measure"),
    ("_cnt", "measure"),
    ("_count", "measure"),
    ("_amount", "measure"),
    ("_amt", "measure"),
    ("_value", "measure"),
    ("_price", "measure"),
    ("_rate", "measure"),
    ("_balance", "measure"),
    ("_total", "measure"),
]

# 完全忽略的技术列（ETL 时间戳、租户等）
_IGNORE_EXACT = {
    "etl_load_time", "etl_time", "dw_batch_id", "dw_etl_time",
    "update_time", "create_time", "id",
}


@dataclass
class ColumnMeta:
    name: str
    dtype: str
    role: str = "other"          # key / label / category / status / datetime / measure / other
    family: str | None = None    # "material" for material_code / material_name

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.dtype, "role": self.role}


@dataclass
class TableMeta:
    schema: str
    name: str
    columns: list[ColumnMeta] = field(default_factory=list)

    @property
    def layer(self) -> str:
        return LAYERS.get(self.schema, "unknown")

    @property
    def fq_name(self) -> str:
        return f"{self.schema}.{self.name}"

    def families(self) -> dict[str, list[ColumnMeta]]:
        """按列名前缀家族分组：{material: [material_code, material_name]}。"""
        out: dict[str, list[ColumnMeta]] = {}
        for c in self.columns:
            if c.family:
                out.setdefault(c.family, []).append(c)
        return out

    def columns_of(self, role: str) -> list[ColumnMeta]:
        return [c for c in self.columns if c.role == role]

    def date_column(self) -> str | None:
        """这张表的时间列（用于判断是不是快照表）。"""
        preferred = ("record_date", "data_date", "stat_date", "stat_month",
                     "date_key", "production_month", "period")
        names = {c.name.lower(): c.name for c in self.columns}
        for p in preferred:
            if p in names:
                return names[p]
        for c in self.columns:
            if c.role == "datetime":
                return c.name
        return None


def classify_column(name: str) -> tuple[str, str | None]:
    """列名 → (语义角色, 前缀家族)。"""
    low = name.lower()
    if low in _IGNORE_EXACT:
        return "ignore", None
    for suffix, role in _ROLE_SUFFIXES:
        if low.endswith(suffix):
            family = low[: -len(suffix)]
            # family 为空说明列名就是 "_code" 这种，去掉
            return role, (family or None)
    return "other", None


def _row_estimate(cur, schema: str, table: str) -> int | None:
    """information_schema 里的行数估计（Doris 不保证精确）。"""
    try:
        cur.execute(
            "SELECT table_rows FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def _with_doris_conn(db):
    """拿到 doris-dwh 的裸连接（凭据在 vault 里，不在这里解密码）。

    返回一个上下文管理器；调用方用 `with _with_doris_conn(db) as raw:`。
    """
    from app.models.connection import Connection
    from app.services.data_plane.connection_service import ConnectionService

    conn_row = db.query(Connection).filter(Connection.name == "doris-dwh").first()
    if conn_row is None:
        raise RuntimeError("找不到名为 doris-dwh 的数据库连接，无法扫描表结构")
    return ConnectionService(db).with_conn(conn_row.id)


def scan(db, schemas: tuple[str, ...] = tuple(LAYERS)) -> list[TableMeta]:
    """扫描指定 schema 的全部表与列。"""
    placeholders = ",".join(["%s"] * len(schemas))
    tables: dict[tuple[str, str], TableMeta] = {}

    with _with_doris_conn(db) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_schema IN ({placeholders}) "
            f"ORDER BY table_schema, table_name",
            schemas,
        )
        for s, t in cur.fetchall():
            tables[(s, t)] = TableMeta(schema=s, name=t)

        cur.execute(
            f"SELECT table_schema, table_name, column_name, data_type "
            f"FROM information_schema.columns "
            f"WHERE table_schema IN ({placeholders}) "
            f"ORDER BY table_schema, table_name, ordinal_position",
            schemas,
        )
        for s, t, col, dtype in cur.fetchall():
            meta = tables.get((s, t))
            if meta is None:
                continue
            role, family = classify_column(col)
            meta.columns.append(ColumnMeta(name=col, dtype=dtype, role=role, family=family))
        cur.close()

    _attach_unsuffixed_columns(list(tables.values()))
    return list(tables.values())


def _attach_unsuffixed_columns(tables: list[TableMeta]) -> None:
    """把"没有后缀但前缀属于已知家族"的列挂到那个家族上。

    `material_weight` / `aging_days` 这类列不以后缀结尾，按后缀分类只能得到
    role=other、family=None。它们其实属于 material 家族，是实体上有意义的属性。

    做法：先由带后缀的列统计出"有哪些家族"，第二遍再把 other 列按**首词**
    匹配进去。只认已存在的家族，不会凭空造新家族。
    """
    known: set[str] = set()
    for t in tables:
        for c in t.columns:
            if c.family:
                known.add(c.family)

    for t in tables:
        for c in t.columns:
            if c.family or c.role != "other":
                continue
            low = c.name.lower()
            head = low.split("_")[0]
            if head in known:
                c.family = head


def sample_key_uniqueness(
    db, schema: str, table: str, column: str, limit: int = 20000,
) -> dict:
    """抽样判断某一列是不是真的"一个实体一行"。

    带 LIMIT，不扫全表。返回 {sampled, distinct, ratio}。
    只在需要确认候选实体的 key 时才调用，不是每列都调。
    """
    import re as _re

    if not _re.fullmatch(r"[A-Za-z0-9_]+", column) or not _re.fullmatch(r"[A-Za-z0-9_]+", table):
        return {"sampled": 0, "distinct": 0, "ratio": 0.0}
    sql = (
        f"SELECT COUNT(*), COUNT(DISTINCT {column}) FROM "
        f"(SELECT {column} FROM {schema}.{table} LIMIT {int(limit)}) t"
    )
    try:
        with _with_doris_conn(db) as conn:
            cur = conn.cursor()
            cur.execute(sql)
            row = cur.fetchone()
            cur.close()
        sampled, distinct = int(row[0] or 0), int(row[1] or 0)
        return {
            "sampled": sampled,
            "distinct": distinct,
            "ratio": round(distinct / sampled, 4) if sampled else 0.0,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("抽样失败 %s.%s: %s", table, column, e)
        return {"sampled": 0, "distinct": 0, "ratio": 0.0, "error": str(e)[:120]}
