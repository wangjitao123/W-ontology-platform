"""口径同步 —— 让对象的过滤条件跟着 Cube 的模型自动走。

要解决的问题：
    Cube 的 cube 常常在 SQL 里带过滤，例如
        contract_vehicle: SELECT * FROM dwh_dwd.dwd_crm_contract_vehicle_di
                         WHERE contract_deleted = 0 AND sign_state = 4
    元枢的对象类型也有一个 ``config_json.filter_expr``，用来把对象集合收窄到同一口径。
    这两者必须一致，否则会出现「对象存在、但指标查出来是 0」——本轮之前就有 42%
    的合同对象是这个状况。

    手写一遍就够了吗？不够。Cube 的 yml 一改，元枢这边不会知道，又漂了。
    所以这里把「从 cube 模型派生 filter_expr」做成一个可重复执行的同步动作。

设计原则：
    1. **只处理能安全解析的 cube**。带 CTE / UNION / JOIN / GROUP BY / 子查询的 SQL
       一律跳过并给出原因——那种情况下「口径」不等于「一句 WHERE」，硬抄会抄错。
    2. **dry-run 优先**。默认只报告「会改什么」，不落库。
    3. **可人工接管**。对象上标了 ``filter_expr_manual: true`` 的，同步不覆盖。
    4. 只替换 ``filter_expr`` 这一个键，其余 config_json 原样保留。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import OntologyEntity
from app.models.object_binding import ObjectBinding

logger = logging.getLogger(__name__)

# 这些关键字一出现，说明 cube 的 SQL 不是「一句 SELECT + WHERE」，口径不能直接抄
_UNSAFE_TOKENS = (
    " WITH ", " UNION ", " JOIN ", " GROUP BY ", " HAVING ", " DISTINCT ",
    " OVER (", " ORDER BY ",
)

_SIMPLE_SELECT = re.compile(
    r"^\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<table>[A-Za-z0-9_.]+)"
    r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z0-9_]+))?"
    r"(?:\s+WHERE\s+(?P<where>.+?))?\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class FilterSyncRow:
    object_type: str
    object_display: str
    cube: str | None
    table: str | None
    old_filter: str | None
    new_filter: str | None
    action: str          # apply | unchanged | skip | manual
    reason: str | None = None


@dataclass
class FilterSyncReport:
    dry_run: bool
    model_dir: str
    cubes_scanned: int = 0
    cubes_usable: int = 0
    rows: list[FilterSyncRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for r in self.rows:
            counts[r.action] = counts.get(r.action, 0) + 1
        return {
            "dryRun": self.dry_run,
            "modelDir": self.model_dir,
            "cubesScanned": self.cubes_scanned,
            "cubesUsable": self.cubes_usable,
            "summary": counts,
            "changes": [r.__dict__ for r in self.rows],
        }


def _norm(sql: Any) -> str | None:
    if not isinstance(sql, str):
        return None
    s = " ".join(sql.split())
    return s or None


def load_cube_sqls(model_dir: Path) -> dict[str, str]:
    """从 cube 模型目录读出 {cube 名: sql}。目录不存在则返回空。"""
    if not model_dir.is_dir():
        return {}

    import yaml

    out: dict[str, str] = {}
    for path in sorted(model_dir.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("口径同步: 解析 %s 失败: %s", path.name, e)
            continue
        for cube in doc.get("cubes") or []:
            name = cube.get("name")
            sql = _norm(cube.get("sql"))
            if name and sql:
                out[str(name)] = sql
    return out


def extract_filter(sql: str) -> tuple[str | None, str | None, str | None]:
    """从 cube 的 SQL 里抽出 (表名, WHERE 条件, 跳过原因)。

    只在 SQL 是「单表 SELECT + WHERE」时返回过滤条件；否则返回原因。
    """
    upper = f" {sql.upper()} "
    for token in _UNSAFE_TOKENS:
        if token in upper:
            return None, None, f"SQL 含 {token.strip()}，口径不是单条 WHERE"
    if "(SELECT" in upper.replace(" ", "") or "( SELECT" in upper:
        return None, None, "SQL 含子查询"

    m = _SIMPLE_SELECT.match(sql)
    if not m:
        return None, None, "SQL 不是简单 SELECT ... FROM ... [WHERE ...]"

    table = (m.group("table") or "").split(".")[-1]
    where = _norm(m.group("where"))
    if where:
        # 去掉表别名前缀（t.xxx -> xxx），对象的 filter_expr 作用在未加别名的表上
        alias = m.group("alias")
        if alias and alias.upper() not in ("WHERE", "GROUP", "ORDER", "LIMIT"):
            where = re.sub(rf"\b{re.escape(alias)}\.", "", where)
    return table, where, None


def _primary_asset_table(db: Session, object_type_id: str) -> str | None:
    binding = (
        db.query(ObjectBinding)
        .filter(
            ObjectBinding.object_type_id == object_type_id,
            ObjectBinding.role == "primary",
        )
        .first()
    )
    if binding is None:
        return None
    from app.models.asset import Asset

    asset = db.get(Asset, binding.asset_id)
    if asset is None:
        return None
    return (asset.locator or {}).get("table")


def sync(db: Session, model_dir: Path, *, dry_run: bool = True) -> FilterSyncReport:
    """把 cube 模型里的 WHERE 同步到对应对象类型的 filter_expr。

    匹配方式是「物理表名」：对象的主绑定指向哪张表，就找 FROM 这张表的 cube。
    """
    report = FilterSyncReport(dry_run=dry_run, model_dir=str(model_dir))
    cube_sqls = load_cube_sqls(model_dir)
    report.cubes_scanned = len(cube_sqls)

    # 表名 -> (cube 名, WHERE)  只留能用简单解析的
    by_table: dict[str, tuple[str, str | None]] = {}
    for cube_name, sql in cube_sqls.items():
        table, where, reason = extract_filter(sql)
        if table is None:
            logger.debug("口径同步: cube %s 跳过（%s）", cube_name, reason)
            continue
        report.cubes_usable += 1
        # 同一张表被多个 cube 用到时，只有全都没有 WHERE 才算「无过滤」
        prev = by_table.get(table)
        if prev is None:
            by_table[table] = (cube_name, where)
        elif prev[1] is None and where is None:
            by_table[table] = prev
        elif prev[1] != where:
            # 口径有冲突（同表不同过滤），保守起见不自动同步
            by_table[table] = (prev[0], "__CONFLICT__")

    for entity in db.query(OntologyEntity).order_by(OntologyEntity.id).all():
        cfg = dict(entity.config_json or {})
        table = _primary_asset_table(db, entity.id)
        old = cfg.get("filter_expr")

        if cfg.get("filter_expr_manual"):
            report.rows.append(FilterSyncRow(
                entity.id, entity.name_cn, None, table, old, None, "manual",
                "已标记 filter_expr_manual，同步不覆盖",
            ))
            continue

        if not table:
            report.rows.append(FilterSyncRow(
                entity.id, entity.name_cn, None, None, old, None, "skip",
                "没有主绑定，无法确定物理表",
            ))
            continue

        hit = by_table.get(table)
        if hit is None:
            report.rows.append(FilterSyncRow(
                entity.id, entity.name_cn, None, table, old, None, "skip",
                "没有 cube 从这张表出发（或 cube 的 SQL 无法安全解析）",
            ))
            continue

        cube_name, new = hit
        if new == "__CONFLICT__":
            report.rows.append(FilterSyncRow(
                entity.id, entity.name_cn, cube_name, table, old, None, "skip",
                "同表有多个 cube 但过滤条件不同，需人工决定",
            ))
            continue

        if (old or None) == (new or None):
            report.rows.append(FilterSyncRow(
                entity.id, entity.name_cn, cube_name, table, old, new, "unchanged",
            ))
            continue

        report.rows.append(FilterSyncRow(
            entity.id, entity.name_cn, cube_name, table, old, new, "apply",
        ))
        if not dry_run:
            cfg["filter_expr"] = new
            if "slice_label" not in cfg and new:
                cfg["filter_expr_source"] = f"cube:{cube_name}"
            cfg["filter_synced_at"] = datetime.utcnow().isoformat()
            entity.config_json = cfg

    if not dry_run:
        db.commit()
    return report
