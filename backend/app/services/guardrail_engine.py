"""护栏引擎 —— 在查询执行前检查是否违反已知约束。

## 为什么必须有这一层

约束写在库里只是"说明文档"。真正能防住算错数的是**执行前检查**。

本轮就抓到一个活例子：`wms_detail` cube 的 SQL 是
`SELECT * FROM dwd_sap_inv_wms_aging_detail_1da`（无日期过滤），
而源表有 143 个快照日期。对它做 SUM 会得到 **89 倍**膨胀的数字：

    跨 143 天直接 SUM : 159,676,855,179.86
    只取最新快照日 SUM :   1,792,767,028.62

`sem_c_aging_snapshot_grain` 这条约束早就写着这个风险，但没人执行它。
本模块把它变成检查。

## 检测器（声明式，存在 sem_constraints.detector_json 里）

    snapshot_aggregate_without_date   对多日快照做聚合却没限定日期
    identity_by_name                  用名称当身份（COUNT/GROUP BY 名称列）
    required_scope_filter             查了某表/某 cube 却缺少必须的口径过滤
    forbidden_drilldown               对聚合级 cube 按明细维度下钻
    cross_namespace_join              把编码空间不相交的两张表按同一列 JOIN

## 判定与拦截策略

每个检测返回 `confidence`：
    high —— 规则与查询文本/结构确定匹配，可以拦
    low  —— 可能命中（比如 SQL 里有子查询），只提示

**只有 `severity=block` + `enforce=true` + `confidence=high` 才拒绝执行。**
其余情况照常执行但附带警告——误拦比漏拦更影响可用性，
所以把"宽进严出"落在 confidence 上。

违反拦截的结果里带 `bypassable: true`，调用方可以传 force 强制看数。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.semantic import SemanticConstraint

logger = logging.getLogger(__name__)

_AGG_RE = re.compile(r"\b(SUM|AVG|COUNT|MAX|MIN)\s*\(", re.IGNORECASE)
# 跨天会重复累加的聚合：SUM / AVG。COUNT(DISTINCT x) 不会（去重了）。
_RISKY_AGG_RE = re.compile(r"\b(SUM|AVG)\s*\(", re.IGNORECASE)
_COUNT_DISTINCT_RE = re.compile(r"\bCOUNT\s*\(\s*DISTINCT\b", re.IGNORECASE)
_PLAIN_COUNT_RE = re.compile(r"\bCOUNT\s*\(", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bJOIN\s+([A-Za-z0-9_.]+)", re.IGNORECASE)
_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z0-9_.]+)", re.IGNORECASE)


@dataclass
class Verdict:
    constraint_id: str
    name: str
    severity: str
    violated: bool
    confidence: str          # high / low
    enforced: bool
    reason: str = ""
    remedy: str = ""
    detector: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraintId": self.constraint_id,
            "name": self.name,
            "severity": self.severity,
            "violated": self.violated,
            "confidence": self.confidence,
            "enforced": self.enforced,
            "reason": self.reason,
            "remedy": self.remedy,
            "detector": self.detector,
        }


@dataclass
class GuardrailResult:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def blocking(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.violated and v.enforced and v.confidence == "high"]

    @property
    def warnings(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.violated and v not in self.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": bool(self.blocking),
            "blocking": [v.to_dict() for v in self.blocking],
            "warnings": [v.to_dict() for v in self.warnings],
        }


def _norm(sql: str) -> str:
    return " ".join((sql or "").split())


def _contains_table(sql: str, tables: list[str]) -> bool:
    low = sql.lower()
    return any(t.lower() in low for t in tables)


# ── 各个检测器 ────────────────────────────────────────────

def _detect_snapshot_aggregate_without_date(
    spec: dict, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> tuple[bool, str, str, str]:
    """对多日快照做聚合却没限定日期。"""
    tables = spec.get("tables") or []
    cubes = spec.get("cubes") or []
    date_col = spec.get("date_column") or "record_date"

    if sql:
        if not _contains_table(sql, tables):
            return False, "", "", "high"
        if not _AGG_RE.search(sql):
            return False, "", "", "high"
        # 出现 date_col 就认为做了限定（等值/子查询/分组都算）
        if re.search(re.escape(date_col), sql, re.IGNORECASE):
            return False, "", "", "high"

        # 区分聚合的"危险程度"，否则会误拦。
        # COUNT(DISTINCT 编码) 跨天不会重复计数 —— 安全，不拦。
        # SUM/AVG 跨天会累加 —— 高危，必须拦。
        # COUNT(*) 跨天得到的是"所有快照行的行数"，语义上不对，但不是数值膨胀，只提示。
        if _RISKY_AGG_RE.search(sql) and not _COUNT_DISTINCT_RE.search(sql):
            return (
                True,
                f"SQL 对 {', '.join(tables)} 做 SUM/AVG，但没有出现 {date_col}，"
                f"而该表跨多个快照日——结果会被重复累加（实测膨胀 89 倍）。",
                f"先按最新快照日限定（如 WHERE {date_col} = (SELECT MAX({date_col}) ...)）再聚合。",
                "high",
            )
        if _PLAIN_COUNT_RE.search(sql):
            return (
                True,
                f"SQL 对 {', '.join(tables)} 做 COUNT，但没有限定 {date_col}；"
                f"得到的是全部快照日的行数，不是当前库存条数。",
                f"加上 {date_col} 限定，或改用 COUNT(DISTINCT 编码列) 统计实体数。",
                "low",   # 只提示，不拦
            )
        return False, "", "", "high"

    if cube_query is not None:
        if cube_name not in cubes:
            return False, "", "", "high"
        measures = [m for m in (cube_query.get("measures") or []) if isinstance(m, str)]
        if not measures:
            return False, "", "", "high"
        # 有按日期的过滤或分组就算限定
        flat = str(cube_query).lower()
        if date_col.lower() in flat:
            return False, "", "", "high"
        # 计数类指标（*_count / *qty 里明确是计数的）跨天求和同样会膨胀，
        # 但 cube 的 measure 全部是 SUM 型聚合，所以这里一律算高危。
        return (
            True,
            f"查询 {cube_name} 的指标 {measures}，但没有对 {date_col} 做任何过滤或分组；"
            f"该 cube 的源表跨多个快照日，直接聚合会重复累加。",
            f"加上 {date_col} 过滤（取最新快照日），或按 {date_col} 分组后再对比。",
            "high",
        )

    return False, "", "", "high"


def _detect_identity_by_name(
    spec: dict, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> tuple[bool, str, str, str]:
    """用名称当身份。"""
    name_cols = spec.get("name_columns") or []
    if not sql or not name_cols:
        return False, "", "", "high"
    for col in name_cols:
        # COUNT(DISTINCT name) 或 GROUP BY name
        if re.search(rf"COUNT\s*\(\s*DISTINCT\s+{re.escape(col)}\s*\)", sql, re.IGNORECASE):
            return (
                True,
                f"SQL 用 COUNT(DISTINCT {col}) 统计实体数，但 {col} 是名称列——"
                f"同一个名称可能对应多个编码，这样统计会偏低。",
                "身份一律用编码列（如 material_code / kunnr），名称只用于展示。",
                "high",
            )
        if re.search(rf"GROUP\s+BY\s+[^;]*\b{re.escape(col)}\b", sql, re.IGNORECASE):
            return (
                True,
                f"SQL 按名称列 {col} 分组，可能把不同编码的同名实体合并。",
                "按编码列分组；需要按名称展示时用编码分组后再取名称。",
                "low",
            )
    return False, "", "", "high"


def _detect_required_scope_filter(
    spec: dict, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> tuple[bool, str, str, str]:
    """查了某表/cube 却缺少必须的口径过滤。"""
    tables = spec.get("tables") or []
    cubes = spec.get("cubes") or []
    required_sql = spec.get("required_sql") or []
    required_filters = spec.get("required_filters") or []

    if sql:
        if not _contains_table(sql, tables):
            return False, "", "", "high"
        missing = [p for p in required_sql if not re.search(p, sql, re.IGNORECASE)]
        if not missing:
            return False, "", "", "high"
        return (
            True,
            f"SQL 查询 {', '.join(tables)}，但缺少口径条件：{', '.join(missing)}。",
            "补上这些条件，否则会把已删除/未审核的数据算进来。",
            "high",
        )

    if cube_query is not None:
        if cube_name not in cubes:
            return False, "", "", "high"
        flat = str(cube_query)
        # cube 已内置口径时（YAML 里 WHERE 写死），不需要调用方再传
        if spec.get("scope_builtin"):
            return False, "", "", "high"
        missing = [m for m in required_filters if m not in flat]
        if not missing:
            return False, "", "", "high"
        return (
            True,
            f"查询 {cube_name} 但缺少口径过滤：{', '.join(missing)}。",
            "补上这些过滤成员。",
            "low",
        )
    return False, "", "", "high"


def _detect_forbidden_drilldown(
    spec: dict, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> tuple[bool, str, str, str]:
    """对聚合级 cube 按明细维度下钻。"""
    cubes = spec.get("cubes") or []
    forbidden = spec.get("forbidden_members") or []
    if cube_query is None or cube_name not in cubes or not forbidden:
        return False, "", "", "high"
    members: list[str] = []
    for key in ("dimensions", "filters", "timeDimensions"):
        for m in cube_query.get(key) or []:
            if isinstance(m, str):
                members.append(m)
            elif isinstance(m, dict):
                members.append(str(m.get("member") or m.get("dimension") or ""))
    hit = [m for m in members if any(f in m for f in forbidden)]
    if not hit:
        return False, "", "", "high"
    return (
        True,
        f"{cube_name} 是聚合级数据（每个报表期一行，全集团合并口径），"
        f"但查询按 {hit} 下钻了——这个切片不存在。",
        "财务指标只能按 period 切片；分客户/分物料请用对应的明细 cube。",
        "high",
    )


def _detect_cross_namespace_join(
    spec: dict, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> tuple[bool, str, str, str]:
    """把编码空间不相交的两张表按同一列 JOIN。"""
    tables = spec.get("tables") or []
    column = spec.get("column") or ""
    if not sql or len(tables) < 2 or not column:
        return False, "", "", "high"
    low = sql.lower()
    if not all(t.lower() in low for t in tables):
        return False, "", "", "high"
    # 两张表都出现且按 column 相连
    joined = [t for t in _JOIN_RE.findall(sql)]
    from_t = [t for t in _FROM_RE.findall(sql)]
    involved = {t.split(".")[-1].lower() for t in joined + from_t}
    if not {t.split(".")[-1].lower() for t in tables} <= involved:
        return False, "", "", "high"
    if not re.search(re.escape(column), sql, re.IGNORECASE):
        return False, "", "", "high"
    return (
        True,
        f"SQL 同时关联了 {', '.join(tables)} 并按 {column} 连接，"
        f"但这两张表的 {column} 编码空间不相交（交集为 0），关联结果必然为空或错配。",
        "分开统计；两者没有可通过该列建立的业务关系。",
        "high",
    )


def _detect_stock_measure_summed(
    spec: dict, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> tuple[bool, str, str, str]:
    """存量度量被跨期求和。

    与 snapshot_aggregate_without_date 的区别：那个针对"同一实体每天重复一份
    全量快照"的表（库龄/资金余额），修法是在 cube 里锁最新日；
    这个针对"表里同时混着存量和流量"的情况（如排产明细里 plan_qty 是流量、
    opening_inventory 是存量），cube 必须保留多月视图，所以只能在查询侧拦。

    实测：otd_schedule_detail.opening_inventory 跨 44 个生产月求和 = 4.2 倍膨胀
    （11,717 vs 最新月 2,765），而同一张表的 plan_qty 跨月求和是对的。
    """
    cubes = spec.get("cubes") or []
    stock_measures = spec.get("measures") or []
    required_member = spec.get("required_member") or ""
    if not stock_measures:
        return False, "", "", "high"

    if cube_query is not None:
        if cube_name not in cubes:
            return False, "", "", "high"
        used = [
            m for m in (cube_query.get("measures") or [])
            if isinstance(m, str) and any(s in m for s in stock_measures)
        ]
        if not used:
            return False, "", "", "high"
        flat = str(cube_query).lower()
        if required_member and required_member.lower() in flat:
            return False, "", "", "high"
        return (
            True,
            f"{cube_name} 的 {used} 是存量字段（期初/余额类），"
            f"跨 {required_member or '期'} 求和会把多个期的存量叠加。",
            f"加上 {required_member} 限定到单个期；看存量趋势要按 {required_member} 分组，"
            f"不能一次性 SUM。",
            "high",
        )

    if sql:
        hits = [s for s in stock_measures if s.lower() in (sql or "").lower()]
        if not hits:
            return False, "", "", "high"
        if required_member and re.search(re.escape(required_member), sql, re.IGNORECASE):
            return False, "", "", "high"
        return (
            True,
            f"SQL 求和了存量字段 {hits}，但没有限定 {required_member}。",
            f"加上 {required_member} 过滤后再求和。",
            "high",
        )
    return False, "", "", "high"


_DETECTORS = {
    "snapshot_aggregate_without_date": _detect_snapshot_aggregate_without_date,
    "identity_by_name": _detect_identity_by_name,
    "required_scope_filter": _detect_required_scope_filter,
    "forbidden_drilldown": _detect_forbidden_drilldown,
    "cross_namespace_join": _detect_cross_namespace_join,
    "stock_measure_summed": _detect_stock_measure_summed,
}


# ── 对外入口 ──────────────────────────────────────────────

def _run(
    db: Session, *, sql: str | None, cube_query: dict | None, cube_name: str,
) -> GuardrailResult:
    result = GuardrailResult()
    rows = (
        db.query(SemanticConstraint)
        .filter(
            SemanticConstraint.status == "active",
            SemanticConstraint.detector_json.isnot(None),
        )
        .all()
    )
    for c in rows:
        spec = c.detector_json or {}
        dtype = spec.get("type")
        fn = _DETECTORS.get(dtype)
        if fn is None:
            continue
        try:
            violated, reason, remedy, confidence = fn(
                spec, sql=sql, cube_query=cube_query, cube_name=cube_name,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("护栏检测 %s 失败: %s", c.id, e)
            continue
        if not violated:
            continue
        result.verdicts.append(Verdict(
            constraint_id=c.id,
            name=c.name,
            severity=c.severity,
            violated=True,
            confidence=confidence,
            enforced=bool(c.enforce) and c.severity == "block",
            reason=reason,
            remedy=remedy or c.remedy,
            detector=dtype,
        ))
    # 拦截项排前面
    result.verdicts.sort(key=lambda v: (not v.enforced, v.severity != "block"))
    return result


def check_cube_query(
    db: Session, query: dict[str, Any], cube_name: str,
) -> GuardrailResult:
    """在 Cube 查询执行前检查。"""
    return _run(db, sql=None, cube_query=query, cube_name=cube_name)


def check_sql(db: Session, sql: str) -> GuardrailResult:
    """临时分析生成的 SQL 检查（生成后才能查，所以是执行后的复核）。"""
    return _run(db, sql=_norm(sql), cube_query=None, cube_name="")
